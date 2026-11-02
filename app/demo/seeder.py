"""Seed one realistic Indian organization that uses every module.

The seeder is a client of the application's own HTTP API (in-process, through Starlette's
``TestClient``), not a second writer. Every record therefore passes the same validation,
permission checks, state machines and audit writes as production traffic, and the demo cannot
drift from the business rules. The only direct inserts are the ones the API deliberately
cannot perform: the organization shell, user accounts (``SELF`` cannot be granted through the
API), module switches and the configurable permissions that expose those modules to employees.

Idempotent: it is a no-op once the demo organization exists. Development only — it creates
accounts with a known password, which ``Settings`` refuses in staging/production.
"""

from __future__ import annotations

import logging
import time as time_module
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.core.clock import today_in, utc_now
from app.core.permissions import Role
from app.core.security import hash_password
from app.db.session import dispose_engine, get_session_factory, unit_of_work
from app.db.types import uuid7
from app.demo import data
from app.models.access import OrganizationRolePermission, UserOrganizationAccess
from app.models.organization import EntityType, Organization, OrganizationSettings
from app.models.platform import AuthProvider, User, UserStatus
from app.models.platform import Role as RoleRow
from app.models.privacy import EmployeePrivacyNotice
from app.services.seed_data import seed_organization

logger = logging.getLogger(__name__)

DEMO_ORGANIZATION_CODE: str = data.ORGANIZATION["organization_code"]
DEFAULT_DEMO_PASSWORD = "Demo@Bharat2026"  # noqa: S105 - documented development credential
IST = ZoneInfo("Asia/Kolkata")
API = "/api/v1"

STANDARD_TASK = {"phase": "PRE_JOINING", "task_type": "CHECKLIST", "owner_type": "HR_COORDINATOR"}
ONBOARDING_TASKS: list[dict[str, Any]] = [
    {
        **STANDARD_TASK,
        "task_code": "KYC_CHECK",
        "title": "Verify identity documents",
        "blocks_activation": True,
        "is_waivable": True,
        "due_offset_days": -3,
    },
    {
        **STANDARD_TASK,
        "task_code": "SETUP",
        "title": "Prepare laptop and email account",
        "due_offset_days": 1,
    },
    {
        **STANDARD_TASK,
        "task_code": "WELCOME_KIT",
        "title": "Hand over the welcome kit",
        "is_mandatory": False,
        "is_waivable": True,
        "due_offset_days": 2,
    },
    {
        "phase": "PRE_JOINING",
        "task_type": "ACKNOWLEDGEMENT",
        "owner_type": "EMPLOYEE",
        "task_code": "PRIVACY",
        "title": "Acknowledge the privacy notice",
        "acknowledgement_kind": "PRIVACY_NOTICE",
        "policy_version_label": "2026.1",
        "privacy_notice_basis": "NOTICE_ONLY",
        "visible_to_employee": True,
        "acknowledgement_statement": "I have read the privacy notice",
        "due_offset_days": -5,
    },
    {
        "phase": "PRE_JOINING",
        "task_type": "DATA_SUBMISSION",
        "owner_type": "EMPLOYEE",
        "task_code": "CONTACT",
        "title": "Provide your contact details",
        "data_section_code": "CONTACT",
        "reviewer_type": "HR_COORDINATOR",
        "visible_to_employee": True,
        "due_offset_days": -7,
    },
    {
        "phase": "JOINING_DAY",
        "task_type": "CHECKLIST",
        "owner_type": "HR_COORDINATOR",
        "task_code": "BUDDY",
        "title": "Assign an onboarding buddy",
        "is_mandatory": False,
        "is_waivable": True,
        "due_offset_days": 0,
    },
]
ONBOARDING_DEPENDENCIES = [{"task_code": "SETUP", "prerequisite_task_code": "KYC_CHECK"}]


_RETRIES = 5
_BACKOFF_SECONDS = 2


class DemoSeedError(RuntimeError):
    """The API refused a seeding request; the message carries the endpoint and response."""


@dataclass(slots=True)
class DemoSeedReport:
    seeded: bool
    skip_reason: str | None = None
    organization_id: uuid.UUID | None = None
    password: str | None = None
    logins: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)


class _Client:
    """A signed-in API caller that fails loudly on an unexpected status."""

    def __init__(self, http: Any, token: str, relogin: Callable[[], str]) -> None:
        self._http = http
        self._headers = {"Authorization": f"Bearer {token}"}
        self._relogin = relogin

    def send(self, method: str, url: str, expect: int, **kwargs: Any) -> Any:
        headers = {**self._headers, **kwargs.pop("headers", {})}
        if method == "POST":
            headers.setdefault("Idempotency-Key", str(uuid.uuid4()))
        response = self._request(method, url, headers, kwargs)
        if response.status_code == 401:
            # A full seed outlasts the 15-minute access token; sign in again and retry
            # (the same Idempotency-Key makes a replayed POST safe).
            self._headers = {"Authorization": f"Bearer {self._relogin()}"}
            headers = {**headers, **self._headers}
            response = self._request(method, url, headers, kwargs)
        if response.status_code != expect:
            raise DemoSeedError(
                f"{method} {url} returned {response.status_code} (expected {expect}): "
                f"{response.text[:400]}"
            )
        return response.json() if response.content else None

    def _request(self, method: str, url: str, headers: dict[str, str], kwargs: Any) -> Any:
        """Send, riding out a dropped database connection (common on a remote server).

        A 5xx is retried after discarding the pooled connections so the next attempt opens a
        fresh one; the Idempotency-Key makes replaying a POST safe. A persistent failure still
        surfaces after ``_RETRIES`` attempts.
        """
        for attempt in range(1, _RETRIES + 1):
            response = self._http.request(method, f"{API}{url}", headers=headers, **kwargs)
            if response.status_code < 500 or attempt == _RETRIES:
                return response
            logger.warning(
                "%s %s -> %s, reconnecting (attempt %d)", method, url, response.status_code, attempt
            )
            dispose_engine()
            time_module.sleep(_BACKOFF_SECONDS * attempt)
        return response  # pragma: no cover - the loop always returns

    def get(self, url: str) -> Any:
        return self.send("GET", url, 200)

    def post(self, url: str, body: Any = None, *, expect: int = 201, **kw: Any) -> Any:
        return self.send("POST", url, expect, json=body if body is not None else {}, **kw)

    def put(self, url: str, body: Any, *, expect: int = 200) -> Any:
        return self.send("PUT", url, expect, json=body)

    def act(
        self, url: str, resource: dict[str, Any], body: Any = None, *, expect: int = 200
    ) -> Any:
        """POST a state transition guarded by ``If-Match`` on the resource's row version."""
        return self.post(
            url, body, expect=expect, headers={"If-Match": str(resource["row_version"])}
        )


def seed_demo(
    app: Any,
    *,
    password: str | None = None,
    also_grant_email: str | None = None,
    today: date | None = None,
) -> DemoSeedReport:
    """Create the demo organization and its data, unless it already exists.

    ``app`` is a built FastAPI application whose database handle is already configured.
    ``also_grant_email`` gives an existing account (the bootstrap administrator) full access
    to the demo organization, so the operator's own login can see it.
    """
    try:
        from fastapi.testclient import TestClient  # noqa: PLC0415 - httpx is a dev dependency
    except ImportError as exc:  # httpx is a dev dependency
        raise DemoSeedError("Demo seeding needs httpx; install the dev extras.") from exc

    factory = get_session_factory()
    session = factory()
    try:
        taken = session.execute(
            select(Organization.id).where(Organization.organization_code == DEMO_ORGANIZATION_CODE)
        ).first()
        if taken:
            # The last phase gives every active person a notice (onboarding touches only a few),
            # so fewer than that means an earlier run was interrupted part-way.
            notified = session.execute(
                select(func.count(func.distinct(EmployeePrivacyNotice.employee_id))).where(
                    EmployeePrivacyNotice.organization_id == taken[0]
                )
            ).scalar_one()
            if notified < len(data.ACTIVE_PEOPLE):
                raise DemoSeedError(
                    f"The demo organization {DEMO_ORGANIZATION_CODE} is only partly loaded "
                    "(an earlier seed was interrupted). Remove it, or drop and recreate the "
                    "development schema, then start again."
                )
            return DemoSeedReport(seeded=False, skip_reason="the demo organization already exists")
        password = password or DEFAULT_DEMO_PASSWORD
        with unit_of_work(session):
            org_id = _create_foundation(session, password, also_grant_email)
    finally:
        session.close()

    http = TestClient(app, raise_server_exceptions=False)
    seeder = _Seeder(http, factory, org_id, password, today or today_in("Asia/Kolkata"))
    seeder.run()
    return DemoSeedReport(
        seeded=True,
        organization_id=org_id,
        password=password,
        logins=seeder.logins,
        counts=seeder.counts,
    )


# ------------------------------------------------------------------------------ foundation


def _email(local: str) -> str:
    return f"{local}@{data.DOMAIN}"


def _create_foundation(session: Session, password: str, also_grant_email: str | None) -> uuid.UUID:
    """The organization shell, its §24.4.1 seed data, and the two administrators."""
    org = data.ORGANIZATION
    organization = Organization(
        id=uuid7(),
        organization_code=org["organization_code"],
        legal_name=org["legal_name"],
        display_name=org["display_name"],
        entity_type=EntityType.PRIVATE_LIMITED.value,
        cin=org["cin"],
        registered_address_line_1=org["address_line_1"],
        registered_address_city=org["city"],
        registered_address_state=org["state"],
        registered_address_state_code=org["state_code"],
        registered_address_country="IN",
        registered_address_postal_code=org["postal_code"],
        financial_year_start_month=4,
        default_currency="INR",
        timezone="Asia/Kolkata",
        status="ACTIVE",
    )
    session.add(organization)
    session.flush()
    seed_organization(session, organization.id)
    admin_roles = (Role.ORGANIZATION_ADMIN, Role.HR_ADMIN)
    _create_user(
        session,
        org_id=organization.id,
        email=_email("hr.admin"),
        name="Meera Krishnan (HR Head)",
        password=password,
        roles=admin_roles,
    )
    _create_user(
        session,
        org_id=organization.id,
        email=_email("director"),
        name="Sanjay Kapoor (Director)",
        password=password,
        roles=admin_roles,
    )
    if also_grant_email:
        existing = session.execute(
            select(User.id).where(User.email == also_grant_email.lower())
        ).scalar_one_or_none()
        if existing is not None:
            _grant(
                session,
                existing,
                organization.id,
                (
                    Role.SYSTEM_SUPER_ADMIN,
                    Role.ORGANIZATION_ADMIN,
                    Role.HR_ADMIN,
                    Role.ATTENDANCE_INTEGRATION_CLIENT,
                ),
            )
    return organization.id


def _grant(
    session: Session, user_id: uuid.UUID, org_id: uuid.UUID, roles: tuple[Role, ...]
) -> None:
    granted_at = utc_now() - timedelta(seconds=1)
    for role in roles:
        role_id = session.execute(
            select(RoleRow.id).where(RoleRow.role_code == role.value)
        ).scalar_one()
        session.add(
            UserOrganizationAccess(
                id=uuid7(),
                user_id=user_id,
                organization_id=org_id,
                role_id=role_id,
                granted_by=None,
                granted_at=granted_at,
                expires_at=None,
                reason=None,
                is_break_glass=False,
            )
        )
    session.flush()


def _create_user(
    session: Session,
    *,
    org_id: uuid.UUID,
    email: str,
    name: str,
    password: str,
    roles: tuple[Role, ...],
    employee_id: uuid.UUID | None = None,
) -> None:
    user = User(
        id=uuid7(),
        email=email,
        display_name=name,
        employee_id=employee_id,
        auth_provider=AuthProvider.LOCAL.value,
        password_hash=hash_password(password),
        mfa_enabled=False,
        status=UserStatus.ACTIVE.value,
    )
    session.add(user)
    session.flush()
    _grant(session, user.id, org_id, roles)


# ------------------------------------------------------------------------------ the seeder


class _Seeder:
    def __init__(
        self,
        http: Any,
        factory: Callable[[], Session],
        org_id: uuid.UUID,
        password: str,
        today: date,
    ) -> None:
        self.http = http
        self.factory = factory
        self.org_id = org_id
        self.password = password
        self.today = today
        self.base = f"/orgs/{org_id}"
        self.logins: list[str] = [_email("hr.admin"), _email("director")]
        self.counts: dict[str, int] = {}
        self.ids: dict[str, Any] = {}
        self.employees: dict[str, dict[str, Any]] = {}
        self.clients: dict[str, _Client] = {}

    # -- plumbing

    def count(self, key: str, n: int = 1) -> None:
        self.counts[key] = self.counts.get(key, 0) + n

    def _token(self, email: str) -> str:
        response = self.http.post(
            f"{API}/auth/login", json={"email": email, "password": self.password}
        )
        if response.status_code != 200:
            raise DemoSeedError(f"login failed for {email}: {response.status_code}")
        return str(response.json()["access_token"])

    def login(self, email: str) -> _Client:
        return _Client(self.http, self._token(email), lambda: self._token(email))

    def db(self, work: Callable[[Session], None]) -> None:
        for attempt in range(1, _RETRIES + 1):
            session = self.factory()
            try:
                with unit_of_work(session):
                    work(session)
                return
            except OperationalError:
                if attempt == _RETRIES:
                    raise
                logger.warning("database connection lost, reconnecting (attempt %d)", attempt)
                dispose_engine()
                time_module.sleep(_BACKOFF_SECONDS * attempt)
            finally:
                session.close()

    def update_settings(self, **values: Any) -> None:
        def work(session: Session) -> None:
            session.execute(
                update(OrganizationSettings)
                .where(OrganizationSettings.organization_id == self.org_id)
                .values(**values)
            )

        self.db(work)

    def working_day(self, offset: int) -> date:
        """The first Monday-Friday on or after ``today + offset`` (before it if negative)."""
        step = 1 if offset >= 0 else -1
        day = self.today + timedelta(days=offset)
        while day.isoweekday() > 5:
            day += timedelta(days=step)
        return day

    def ist(self, day: date, hour: int, minute: int = 0) -> str:
        return datetime.combine(day, time(hour, minute), tzinfo=IST).isoformat()

    def run(self) -> None:
        self.hr = self.login(_email("hr.admin"))
        self.director = self.login(_email("director"))
        self.enable_modules()
        self.structure()
        self.holidays()
        self.people()
        self.job_architecture()
        self.accounts()
        self.leave()
        self.attendance()
        self.salary()
        self.onboarding()
        self.privacy_notices()
        logger.info("demo data loaded", extra={"counts": self.counts})

    # -- 1. switches

    def enable_modules(self) -> None:
        start = date(self.today.year, 1, 1)

        def work(session: Session) -> None:
            session.execute(
                update(OrganizationSettings)
                .where(OrganizationSettings.organization_id == self.org_id)
                .values(
                    enable_leave_module=True,
                    enable_leave_self_service=True,
                    leave_tracking_start_date=start,
                    enable_attendance_module=True,
                    enable_attendance_self_service=True,
                    attendance_tracking_start_date=start,
                    enable_onboarding_module=True,
                    enable_onboarding_self_service=True,
                )
            )
            for code in ("self.leave", "self.attendance", "self.onboarding"):
                session.add(
                    OrganizationRolePermission(
                        id=uuid7(),
                        organization_id=self.org_id,
                        role_code=Role.EMPLOYEE_SELF.value,
                        permission_code=code,
                        enabled_by=None,
                    )
                )

        self.db(work)
        self.hr.put(
            f"{self.base}/settings",
            {
                "org_pan_required": True,
                "allow_custom_salary_components": True,
                "enable_employee_self_view": True,
                "backdated_salary_max_days": 3650,  # long-serving staff are loaded at joining date
                "enable_headcount_reservations": True,
            },
        )

    # -- 2. branches, masters, employee-id rule

    def structure(self) -> None:
        hr, base = self.hr, self.base
        self.ids["branch"] = {
            b["branch_code"]: hr.post(f"{base}/branches", b)["id"] for b in data.BRANCHES
        }
        self.ids["dept"] = {
            code: hr.post(f"{base}/masters/departments", {"code": code, "name": name})["id"]
            for code, name in data.DEPARTMENTS
        }
        hr.post(
            f"{base}/masters/sub-departments",
            {
                "code": "PLATFORM",
                "name": "Platform Engineering",
                "department_id": self.ids["dept"]["ENGG"],
            },
        )
        self.ids["desig"] = {
            code: hr.post(f"{base}/masters/designations", {"code": code, "name": name})["id"]
            for code, name in data.DESIGNATIONS
        }
        self.ids["grade"] = {
            rank: hr.post(
                f"{base}/masters/grades", {"code": f"G{rank}", "name": name, "rank": rank}
            )["id"]
            for rank, name in data.GRADES
        }
        types = hr.get(f"{base}/masters/employment-types")
        self.ids["emp_type"] = next(t["id"] for t in types if t["base_type"] == "PERMANENT")
        self.count("branches", len(data.BRANCHES))
        self.count("masters", len(data.DEPARTMENTS) + len(data.DESIGNATIONS) + len(data.GRADES))
        rule = hr.post(
            f"{base}/employee-id-rules",
            {
                "rule_name": "Bharat Tech employee codes",
                "rule_code": "BTSPL-SERIES",
                "format_pattern": "{ORG}{SEP}{SEQ}",
                "separator": "-",
                "sequence_reset_policy": "NEVER",
                "sequence_start": 1,
                "sequence_increment": 1,
                "sequence_padding_length": 4,
                "effective_from": "2018-01-01",
                "priority": 100,
            },
        )
        hr.post(f"{base}/employee-id-rules/{rule['id']}/publish", expect=200)

    # -- 3. holidays and weekly offs

    def holidays(self) -> None:
        hr, base, year = self.hr, self.base, self.today.year
        calendar = hr.post(
            f"{base}/holiday-calendars",
            {
                "calendar_code": f"IN-{year}",
                "calendar_name": f"India holidays {year}",
                "is_organization_default": True,
            },
        )
        entries = [(date(year, m, d), name, kind) for (m, d), name, kind in data.FIXED_HOLIDAYS]
        entries += list(data.MOVABLE_HOLIDAYS.get(year, ()))
        entries.sort()
        created = [
            hr.post(
                f"{base}/holiday-calendars/{calendar['id']}/holidays",
                {"holiday_date": day.isoformat(), "holiday_name": name, "holiday_type": kind},
            )
            for day, name, kind in entries
        ]
        # One announced holiday that was later withdrawn, to show the cancellation history.
        index = next(i for i, (_, name, _) in enumerate(entries) if name == "Christmas")
        hr.act(
            f"{base}/holiday-calendars/{calendar['id']}/holidays/{created[index]['id']}/cancel",
            created[index],
            {"cancellation_reason": "Replaced by a company-wide floating holiday"},
        )
        self.count("holidays", len(created))
        for branch_id in self.ids["branch"].values():
            hr.post(
                f"{base}/branches/{branch_id}/holiday-calendar-assignments",
                {"calendar_id": calendar["id"], "effective_from": date(year, 1, 1).isoformat()},
            )
        sat_sun = [{"day_of_week": 6, "occurrence": "ALL"}, {"day_of_week": 7, "occurrence": "ALL"}]
        bank = [
            {"day_of_week": 6, "occurrence": "SECOND"},
            {"day_of_week": 6, "occurrence": "FOURTH"},
            {"day_of_week": 7, "occurrence": "ALL"},
        ]
        patterns = (
            ("SATSUN", "Saturday and Sunday", sat_sun),
            ("SUNDAY", "Sunday only (field sales)", [{"day_of_week": 7, "occurrence": "ALL"}]),
            ("BANK", "2nd & 4th Saturday and Sunday", bank),
        )
        self.ids["pattern"] = {
            code: hr.post(
                f"{base}/weekly-off-patterns",
                {"pattern_code": code, "pattern_name": name, "days": days},
            )["id"]
            for code, name, days in patterns
        }
        self.update_settings(default_weekly_off_pattern_id=uuid.UUID(self.ids["pattern"]["SATSUN"]))

    # -- 4. people

    def employee_body(self, person: data.Person, index: int) -> dict[str, Any]:
        branch = next(b for b in data.BRANCHES if b["branch_code"] == person.branch)
        joined = self.today - timedelta(days=person.joined_days_ago)
        body: dict[str, Any] = {
            "first_name": person.first,
            "last_name": person.last,
            "gender": person.gender,
            "date_of_birth": person.dob,
            "nationality": "IN",
            "marital_status": person.marital_status,
            "father_name": person.father,
            "personal_mobile": f"+9198{index:02d}{1000 + index * 37:04d}{index:02d}",
            "official_email": _email(f"{person.first}.{person.last}".lower()),
            "joining_date": joined.isoformat(),
            "employment_type_id": self.ids["emp_type"],
            "department_id": self.ids["dept"][person.department],
            "designation_id": self.ids["desig"][person.designation],
            "grade_id": self.ids["grade"][person.grade],
            "branch_id": self.ids["branch"][person.branch],
            "work_mode": "HYBRID" if person.department == "ENGG" else "ON_SITE",
            "notice_period_days": 60 if person.grade >= 4 else 30,
            "current_address": {
                "line_1": f"{10 + index}, Residency Layout",
                "city": branch["city"],
                "state": branch["state"],
                "postal_code": branch["postal_code"],
            },
            "permanent_address_same_as_current": True,
        }
        if person.reports_to:
            body["reporting_manager_employee_id"] = self.employees[person.reports_to]["id"]
        return {k: v for k, v in body.items() if v is not None}

    def people(self) -> None:
        hr, base = self.hr, self.base
        index = 0
        for person in data.ACTIVE_PEOPLE:
            index += 1
            created = hr.post(f"{base}/employees", self.employee_body(person, index))
            self.employees[person.key] = hr.post(
                f"{base}/employees/{created['id']}/activate", expect=200
            )
            self.count("employees_active")
        for person in data.JOINERS:
            index += 1
            self.employees[person.key] = hr.post(
                f"{base}/employees", self.employee_body(person, index)
            )
            self.count("employees_draft")
        for i, person in enumerate(data.ACTIVE_PEOPLE, start=1):
            self.profile(person, i)
        # Kavita has resigned: a status transition carrying her last working day.
        hr.post(
            f"{base}/employees/{self.employees['kavita']['id']}/status-transitions",
            {
                "to_status": "ON_NOTICE",
                "status_effective_date": self.today.isoformat(),
                "employment_end_date": (self.today + timedelta(days=30)).isoformat(),
                "status_reason": "RESIGNATION",
                "remarks": "Moving to a start-up in Pune",
            },
            expect=200,
        )

    def profile(self, person: data.Person, i: int) -> None:
        """Statutory ids (PAN, UAN), a salary account, education and family for one employee."""
        hr = self.hr
        path = f"{self.base}/employees/{self.employees[person.key]['id']}"
        pan = f"{'ABCDEFGHJK'[i]}{'LMNPQRSTUV'[i]}{'XYZ'[i % 3]}P{person.last[0]}{1000 + i * 111}"
        hr.put(f"{path}/identifiers/PAN", {"value": f"{pan}{'ABCDEFGH'[i - 1]}"})
        # Aadhaar is NOT_COLLECTED by the seeded policy (§11.3), so the demo does not store it.
        hr.put(f"{path}/identifiers/UAN", {"value": f"1008{i:02d}{345600 + i:06d}"})
        joined = self.today - timedelta(days=person.joined_days_ago)
        hr.post(
            f"{path}/bank-accounts",
            {
                "account_holder_name": f"{person.first} {person.last}",
                "bank_name": ("HDFC Bank", "ICICI Bank", "State Bank of India")[i % 3],
                "branch_name": "Main Branch",
                "account_number": f"5010{i:02d}3456789{i}",
                "ifsc_code": ("HDFC0001234", "ICIC0001234", "SBIN0001234")[i % 3],
                "account_type": "SAVINGS",
                "effective_from": joined.isoformat(),
                "is_primary": True,
            },
        )
        hr.post(
            f"{path}/education",
            {
                "qualification_level": "GRADUATE",
                "degree_name": "B.E. / B.Tech",
                "institution_name": "Visvesvaraya Technological University",
                "start_year": 2011 + i % 5,
                "passing_year": 2015 + i % 5,
                "result_type": "PERCENTAGE",
                "result_value": str(68 + i),
                "maximum_result_value": 100,
            },
        )
        hr.post(
            f"{path}/family-members",
            {"full_name": person.father, "relationship_code": "FATHER", "is_dependent": False},
        )
        self.count("profiles")

    # -- 5. positions and headcount

    def job_architecture(self) -> None:
        hr, base = self.hr, self.base
        family = hr.post(
            f"{base}/job-families",
            {"family_code": "TECH", "name": "Technology", "node_level": "FAMILY"},
        )
        jobs = {
            code: hr.post(
                f"{base}/job-catalog",
                {
                    "job_code": code,
                    "title": title,
                    "job_family_id": family["id"],
                    "default_grade_id": self.ids["grade"][rank],
                },
            )
            for code, title, rank in (
                ("SWE", "Software Engineer", 2),
                ("SSWE", "Senior Software Engineer", 3),
                ("ENGMGR", "Engineering Manager", 5),
            )
        }
        fy_start = date(self.today.year, 4, 1).isoformat()
        plan = hr.post(
            f"{base}/headcount-plans",
            {
                "plan_code": "ENG-FY27",
                "name": "Engineering establishment FY27",
                "scope_type": "DEPARTMENT",
                "department_id": self.ids["dept"]["ENGG"],
                "authorized_fte": "10.00",
                "authorized_headcount": 10,
                "effective_from": fy_start,
            },
        )
        hr.post(  # left in DRAFT for review
            f"{base}/headcount-plans",
            {
                "plan_code": "SALES-FY27",
                "name": "Sales expansion FY27 (proposed)",
                "scope_type": "DEPARTMENT",
                "department_id": self.ids["dept"]["SALES"],
                "authorized_fte": "6.00",
                "authorized_headcount": 6,
                "effective_from": fy_start,
            },
        )
        # The approver must differ from the creator (self-approval is refused).
        self.director.act(
            f"{base}/headcount-plans/{plan['id']}/activate",
            plan,
            {"note": "Approved in the annual budget review"},
        )
        positions = {}
        for code, job in (
            ("ENGMGR-01", "ENGMGR"),
            ("SSWE-01", "SSWE"),
            ("SWE-01", "SWE"),
            ("SWE-02", "SWE"),
            ("SWE-03", "SWE"),
        ):
            created = hr.post(
                f"{base}/positions",
                {
                    "position_code": code,
                    "job_id": jobs[job]["id"],
                    "department_id": self.ids["dept"]["ENGG"],
                    "authorized_fte": "1.00",
                    "max_incumbents": 1,
                },
            )
            positions[code] = hr.act(
                f"{base}/positions/{created['id']}/open",
                created,
                {"reason": "Authorized by the FY27 workforce plan"},
            )
        for code, key in (("ENGMGR-01", "rajesh"), ("SSWE-01", "priya"), ("SWE-01", "arjun")):
            hr.post(
                f"{base}/employees/{self.employees[key]['id']}/job-changes",
                {
                    "effective_date": self.today.isoformat(),
                    "change_type": "TRANSFER",
                    "reason": "Mapped to the approved position",
                    "position_change": {
                        "position_id": positions[code]["id"],
                        "assignment_type": "PRIMARY",
                        "fte": "1.00",
                        "end_current_assignment": True,
                        "reason": "Mapped to the approved position",
                    },
                },
                expect=200,
            )
        self.count("positions", len(positions))
        # Growth requests: one approved by the director (applied), one still awaiting a decision.
        for code, approve in (("SWE-03", True), ("SWE-02", False)):
            request = hr.post(
                f"{base}/headcount-change-requests",
                {
                    "position_id": positions[code]["id"],
                    "change_type": "INCREASE",
                    "requested_fte": "2.00",
                    "requested_headcount": 2,
                    "reason": "Approved growth in the platform team for the next quarter",
                },
            )
            request = hr.act(f"{base}/headcount-change-requests/{request['id']}/submit", request)
            if approve:
                self.director.act(
                    f"{base}/headcount-change-requests/{request['id']}/approve",
                    request,
                    {"note": "Within the approved FY27 establishment"},
                )

    # -- 6. employee logins

    def accounts(self) -> None:
        managers = {"rajesh": Role.DEPARTMENT_MANAGER, "vikram": Role.DEPARTMENT_MANAGER}
        staff = ["priya", "arjun", "kavita", "faizan", "ananya", "rohan", "divya", "aditya"]
        roles = {**managers, **dict.fromkeys(staff, Role.EMPLOYEE_SELF)}

        def work(session: Session) -> None:
            for key, role in roles.items():
                person = self.person(key)
                _create_user(
                    session,
                    org_id=self.org_id,
                    email=self.login_email(person),
                    name=f"{person.first} {person.last}",
                    password=self.password,
                    roles=(role,),
                    employee_id=uuid.UUID(self.employees[key]["id"]),
                )

        self.db(work)
        for key in roles:
            email = self.login_email(self.person(key))
            self.logins.append(email)
            self.clients[key] = self.login(email)

    @staticmethod
    def login_email(person: data.Person) -> str:
        return _email(f"{person.first}.{person.last}".lower())

    @staticmethod
    def person(key: str) -> data.Person:
        return next(p for p in (*data.ACTIVE_PEOPLE, *data.JOINERS) if p.key == key)

    # -- 7. leave

    def leave(self) -> None:
        hr, base = self.hr, self.base
        types = {
            code: hr.post(
                f"{base}/leave-types",
                {
                    "leave_type_code": code,
                    "leave_type_name": name,
                    "paid_classification": paid,
                    "tracks_balance": paid == "PAID",
                },
            )
            for code, name, paid in (
                ("CL", "Casual Leave", "PAID"),
                ("SL", "Sick Leave", "PAID"),
                ("EL", "Earned (Privilege) Leave", "PAID"),
                ("ML", "Maternity Leave", "PAID"),
                ("LWP", "Leave Without Pay", "UNPAID"),
            )
        }
        year = date(self.today.year, 1, 1).isoformat()
        common = {
            "effective_from": year,
            "allocation_method": "NONE",
            "approval_chain": "MANAGER_ONLY",
            "allow_half_day": True,
            "employee_cancellation_allowed": True,
        }
        policies = {
            "CL": {**common, "max_days_per_application": "3"},
            "SL": {**common, "document_required_above_days": "2"},
            "EL": {
                **common,
                "carry_forward_enabled": True,
                "max_carry_forward_days": "30",
                "approval_chain": "MANAGER_THEN_HR",
                "min_notice_days": 3,
            },
        }
        for code, version in policies.items():
            policy = hr.post(
                f"{base}/leave-policies",
                {
                    "leave_type_id": types[code]["id"],
                    "policy_code": f"{code}-STD",
                    "policy_name": f"{types[code]['leave_type_name']} policy",
                    "version": version,
                },
            )
            draft = policy["versions"][0]
            hr.act(f"{base}/leave-policies/{policy['id']}/versions/{draft['id']}/publish", draft)
        # The maternity policy is drafted but not yet released.
        hr.post(
            f"{base}/leave-policies",
            {
                "leave_type_id": types["ML"]["id"],
                "policy_code": "ML-STD",
                "policy_name": "Maternity Leave policy (Maternity Benefit Act)",
                "version": {**common, "applies_gender": "FEMALE"},
            },
        )
        self.count("leave_policies", 4)
        for person in data.ACTIVE_PEOPLE:
            for code, days in (("CL", "12"), ("SL", "7"), ("EL", "18")):
                hr.post(
                    f"{base}/employees/{self.employees[person.key]['id']}/leave-transactions",
                    {
                        "transaction_type": "OPENING_BALANCE",
                        "leave_type_id": types[code]["id"],
                        "leave_year_start": year,
                        "quantity_days": days,
                        "effective_date": year,
                        "reason": "Opening balance at the start of the leave year",
                    },
                )
        manager = self.clients["rajesh"]

        def apply(key: str, code: str, start: date, end: date | None = None, **extra: Any) -> Any:
            self.count("leave_applications")
            return self.clients[key].post(
                f"{base}/me/leave-applications",
                {
                    "leave_type_id": types[code]["id"],
                    "start_date": start.isoformat(),
                    "end_date": (end or start).isoformat(),
                    "start_half": "FIRST_HALF",
                    "end_half": "SECOND_HALF",
                    **extra,
                },
            )

        approved = apply("arjun", "CL", self.working_day(6), self.working_day(7))
        manager.act(
            f"{base}/leave-applications/{approved['id']}/approve",
            approved,
            {"comment": "Approved, enjoy the wedding"},
        )
        apply("priya", "EL", self.working_day(12), self.working_day(16))  # stays pending
        rejected = apply("kavita", "CL", self.working_day(4))
        manager.act(
            f"{base}/leave-applications/{rejected['id']}/reject",
            rejected,
            {"decision_comment": "Release freeze this week; please pick another date"},
        )
        withdrawn = apply("arjun", "SL", self.working_day(9))
        self.clients["arjun"].act(
            f"{base}/me/leave-applications/{withdrawn['id']}/withdraw",
            withdrawn,
            {"reason": "Appointment rescheduled"},
        )

    # -- 8. attendance

    def attendance(self) -> None:
        hr, base = self.hr, self.base
        shifts = {
            code: hr.post(
                f"{base}/shifts",
                {
                    "shift_code": code,
                    "shift_name": name,
                    "start_time": start,
                    "end_time": end,
                    "break_minutes": 60,
                    "full_day_min_work_minutes": 480,
                    "half_day_min_work_minutes": 240,
                },
            )
            for code, name, start, end in (
                ("GEN", "General shift (9:00-18:00)", "09:00:00", "18:00:00"),
                ("SALES", "Sales shift (10:00-19:00)", "10:00:00", "19:00:00"),
            )
        }
        fields = {
            "effective_from": date(self.today.year, 1, 1).isoformat(),
            "default_weekly_off_pattern_id": self.ids["pattern"]["SATSUN"],
            "default_shift_id": shifts["GEN"]["id"],
            "break_deduction_mode": "FIXED_FROM_SHIFT",
            "self_punch_enabled": True,
            "integration_punch_enabled": True,
            "on_duty_requests_enabled": True,
            "late_grace_minutes": 10,
            "early_departure_grace_minutes": 10,
        }
        policy = hr.post(
            f"{base}/attendance-policies",
            {"policy_code": "STD", "policy_name": "Standard attendance", "version": fields},
        )
        draft = policy["versions"][0]
        hr.act(f"{base}/attendance-policies/{policy['id']}/versions/{draft['id']}/publish", draft)
        hr.post(  # proposed, not yet released
            f"{base}/attendance-policies",
            {
                "policy_code": "FLEXI",
                "policy_name": "Flexi-time policy (proposed)",
                "version": {**fields, "punch_evaluation_mode": "FIRST_IN_LAST_OUT"},
            },
        )
        for key, shift, pattern in (
            ("vikram", "SALES", "SUNDAY"),
            ("faizan", "SALES", "SUNDAY"),
            ("ananya", "GEN", "BANK"),
        ):
            hr.post(
                f"{base}/employees/{self.employees[key]['id']}/shift-assignments",
                {
                    "shift_id": shifts[shift]["id"],
                    "weekly_off_pattern_id": self.ids["pattern"][pattern],
                    "effective_from": self.employees[key]["joining_date"],
                    "reason": "Role-based roster",
                },
            )
        # Eight recent working days, newest first, none of them today.
        days: list[date] = []
        cursor = self.today
        while len(days) < 8:
            cursor = self.working_day((cursor - self.today).days - 1)
            days.append(cursor)
        gap_day = days[2]
        sales_staff = ("vikram", "faizan")
        for n, day in enumerate(days):
            for i, person in enumerate(data.ACTIVE_PEOPLE):
                start_hour = 10 if person.key in sales_staff else 9
                late = 40 if (n + i) % 5 == 0 else (i * 3) % 12
                self.punch(person.key, day, "IN", start_hour, late)
                if person.key == "arjun" and day == gap_day:
                    continue  # forgot to punch out; regularized below
                self.punch(person.key, day, "OUT", start_hour + 9, (i * 7) % 40)
        # Close the older days (the last five); the most recent ones stay open.
        for person in data.ACTIVE_PEOPLE:
            if person.key == "arjun":
                continue
            hr.post(
                f"{base}/attendance/finalize",
                {
                    "employee_id": self.employees[person.key]["id"],
                    "from": days[-1].isoformat(),
                    "to": days[4].isoformat(),
                },
                expect=200,
            )
        request = self.clients["arjun"].post(
            f"{base}/me/attendance/regularizations",
            {
                "business_date": gap_day.isoformat(),
                "request_type": "MISSING_PUNCH",
                "reason": "Forgot to punch out; left the office at 6 pm after the release",
                "items": [
                    {
                        "item_type": "ADD_PUNCH",
                        "punch_type": "OUT",
                        "punch_at": self.ist(gap_day, 18, 0),
                    }
                ],
            },
        )
        self.clients["rajesh"].act(
            f"{base}/attendance/regularizations/{request['id']}/approve",
            request,
            {"comment": "Confirmed against the release log"},
        )
        self.clients["priya"].post(  # left pending for the manager
            f"{base}/me/attendance/regularizations",
            {
                "business_date": days[1].isoformat(),
                "request_type": "ON_DUTY",
                "reason": "Client workshop at the customer site",
                "items": [
                    {
                        "item_type": "SET_ON_DUTY",
                        "half": "FIRST_HALF",
                        "work_arrangement": "OFFICE",
                    }
                ],
            },
        )
        self.count("attendance_days", len(days))

    def punch(self, key: str, day: date, kind: str, hour: int, minute: int) -> None:
        self.hr.post(
            f"{self.base}/employees/{self.employees[key]['id']}/attendance/punches",
            {
                "punch_at": self.ist(day, hour, minute),
                "punch_type": kind,
                "remarks": "Entered from the biometric register",
            },
            expect=200,
        )

    # -- 9. salary

    def salary(self) -> None:
        hr, base = self.hr, self.base
        # BASIC is seeded with the organization (a system component); only the rest are created.
        components = {
            row["component_code"]: row
            for row in hr.get(f"{base}/salary-components")
            if row["component_code"] == "BASIC"
        }
        for code, name, category, _value_type, period in data.SALARY_COMPONENTS:
            if code == "BASIC":
                continue
            percent = data.PERCENT_OF_BASIC.get(code)
            components[code] = hr.post(
                f"{base}/salary-components",
                {
                    "component_code": code,
                    "component_name": name,
                    "display_name": name,
                    "component_type": (
                        "DEDUCTION" if category == "EMPLOYEE_DEDUCTION_REFERENCE" else "EARNING"
                    ),
                    "component_category": category,
                    "value_type": "PERCENTAGE" if percent else "FIXED_AMOUNT",
                    **({} if percent else {"amount_period": period, "currency": "INR"}),
                    "is_required": code == "HRA",
                },
            )
        templates = {}
        for code, (name, amounts) in data.SALARY_TEMPLATES.items():
            version = hr.post(
                f"{base}/salary-templates",
                {
                    "template_code": code,
                    "template_name": name,
                    "currency": "INR",
                    "effective_from": f"{self.today.year - 8}-01-01",
                    "mappings": [
                        {
                            "component_id": components[c]["id"],
                            **(
                                {"default_value_percentage": data.PERCENT_OF_BASIC[c]}
                                if c in data.PERCENT_OF_BASIC
                                else {"default_value_amount": amount}
                            ),
                            "is_required": c in ("BASIC", "HRA"),
                        }
                        for c, amount in amounts.items()
                    ],
                },
            )
            hr.post(
                f"{base}/salary-templates/{version['template_id']}"
                f"/versions/{version['id']}/publish",
                expect=200,
            )
            templates[code] = version["template_id"]
        by_grade = {1: "JUNIOR", 2: "JUNIOR", 3: "MID", 4: "MGR", 5: "MGR"}
        for person in data.ACTIVE_PEOPLE:
            joined = self.today - timedelta(days=person.joined_days_ago)
            hr.post(
                f"{base}/employees/{self.employees[person.key]['id']}/salary-assignments",
                {
                    "template_id": templates[by_grade[person.grade]],
                    "effective_from": joined.isoformat(),
                    "revision_reason": "NEW_JOINER",
                    "backdate_reason": "Migrated from the legacy payroll register",
                    "confirm": True,
                },
            )
        in_april = self.today >= date(self.today.year, 4, 1)
        next_april = date(self.today.year + (1 if in_april else 0), 4, 1).isoformat()
        # Priya's promotion: confirmed and future-dated, so the timeline shows a SCHEDULED row.
        hr.post(
            f"{base}/employees/{self.employees['priya']['id']}/salary-assignments",
            {
                "template_id": templates["MGR"],
                "effective_from": next_april,
                "revision_reason": "ANNUAL_REVISION",
                "revision_remarks": "Promotion to Lead, effective with the new financial year",
                "confirm": True,
            },
        )
        # Arjun's revision is still being prepared: a DRAFT that is not on the timeline yet.
        hr.post(
            f"{base}/employees/{self.employees['arjun']['id']}/salary-assignments",
            {
                "template_id": templates["MID"],
                "effective_from": next_april,
                "revision_reason": "ANNUAL_REVISION",
                "revision_remarks": "Pending HR calibration",
            },
        )
        self.count("salary_assignments", len(data.ACTIVE_PEOPLE) + 2)

    # -- 10. onboarding

    def onboarding(self) -> None:
        hr, base = self.hr, self.base
        template = hr.post(
            f"{base}/onboarding-templates",
            {
                "template_code": "STD",
                "template_name": "Standard joiner onboarding",
                "version": {"effective_from": f"{self.today.year}-01-01"},
                "tasks": ONBOARDING_TASKS,
                "dependencies": ONBOARDING_DEPENDENCIES,
            },
        )
        draft = template["versions"][0]
        hr.act(
            f"{base}/onboarding-templates/{template['id']}/versions/{draft['id']}/publish", draft
        )
        hr.post(f"{base}/onboarding-templates/{template['id']}/versions")  # v2 stays a DRAFT
        self.update_settings(onboarding_required_for_activation=True)

        instances = {
            key: hr.post(
                f"{base}/employees/{self.employees[key]['id']}/onboarding",
                {"remarks": "Standard joiner journey"},
            )
            for key in ("rohan", "divya", "aditya")
        }
        self.count("onboarding_instances", len(instances))

        def task(key: str, code: str) -> dict[str, Any]:
            fresh = hr.get(f"{base}/onboarding/{instances[key]['id']}")
            return next(t for t in fresh["tasks"] if t["task_code"] == code)

        def hr_do(key: str, code: str, verb: str, body: Any = None) -> None:
            t = task(key, code)
            hr.act(f"{base}/onboarding/{instances[key]['id']}/tasks/{t['id']}/{verb}", t, body)

        def acknowledge(key: str) -> None:
            self.clients[key].post(
                f"{base}/me/onboarding/tasks/{task(key, 'PRIVACY')['id']}/acknowledge"
            )

        def submit_contact(key: str, mobile: str) -> dict[str, Any]:
            submission: dict[str, Any] = self.clients[key].post(
                f"{base}/me/onboarding/tasks/{task(key, 'CONTACT')['id']}/submissions",
                {"payload": {"personal_mobile": mobile}},
            )
            return submission

        # Aditya: everything done, employee activated and onboarding closed.
        hr_do("aditya", "KYC_CHECK", "complete")
        hr_do("aditya", "SETUP", "complete")
        hr_do("aditya", "WELCOME_KIT", "complete")
        acknowledge("aditya")
        submission = submit_contact("aditya", "+919811100001")
        contact = task("aditya", "CONTACT")
        hr.act(
            f"{base}/onboarding/{instances['aditya']['id']}/tasks/{contact['id']}"
            f"/submissions/{submission['id']}/accept",
            submission,
        )
        hr_do("aditya", "BUDDY", "waive", {"waiver_reason": "Buddy assigned by the manager"})
        # Activation first: onboarding can only be closed for an ACTIVE employee.
        hr.post(f"{base}/employees/{self.employees['aditya']['id']}/activate", expect=200)
        hr.act(
            f"{base}/onboarding/{instances['aditya']['id']}/complete",
            hr.get(f"{base}/onboarding/{instances['aditya']['id']}"),
        )
        # Rohan: mid-way. ID check done, notice acknowledged, contact details awaiting review.
        hr_do("rohan", "KYC_CHECK", "complete")
        acknowledge("rohan")
        submit_contact("rohan", "+919811100002")
        # Divya has only just started; her welcome kit was waived because she joins remotely.
        hr_do("divya", "WELCOME_KIT", "waive", {"waiver_reason": "Remote joiner; kit couriered"})

    # -- 11. privacy notices

    def privacy_notices(self) -> None:
        delivered = utc_now().isoformat()
        for person in data.ACTIVE_PEOPLE:
            path = f"{self.base}/employees/{self.employees[person.key]['id']}/privacy-notices"
            notice = self.hr.post(
                path,
                {
                    "notice_version": "2026.1",
                    "basis": "NOTICE_ONLY",
                    "delivered_at": delivered,
                    "method": "ELECTRONIC",
                },
            )
            if person.key != "faizan":  # one notice is left unacknowledged
                self.hr.post(f"{path}/{notice['id']}/acknowledge", expect=200)
        self.count("privacy_notices", len(data.ACTIVE_PEOPLE))


__all__ = ["DEMO_ORGANIZATION_CODE", "DemoSeedError", "DemoSeedReport", "seed_demo"]
