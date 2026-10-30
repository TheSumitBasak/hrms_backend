"""Shared setup for the leave suites (addendum §6).

Everything goes through the real HTTP API except what the API deliberately cannot do: enabling
the module flags, setting an employee's reporting manager, and creating SELF accounts (the
auth schema forbids granting SELF), which use the same direct inserts as the other fixtures.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import update

from app.core.permissions import Role
from app.models.employee import Employee
from app.models.organization import OrganizationSettings
from tests.factories import create_user, enable_configurable_permission
from tests.job_architecture import Architecture, if_match, setup_architecture

SAT_SUN = [{"day_of_week": 6, "occurrence": "ALL"}, {"day_of_week": 7, "occurrence": "ALL"}]


def today() -> date:
    # Asia/Kolkata is UTC+5:30: the organization's "today" can differ from the UTC date.
    from app.core.clock import today_in

    return today_in("Asia/Kolkata")


def working_day(offset: int = 3) -> date:
    """The first Monday-Friday on or after ``today + offset``."""
    day = today() + timedelta(days=offset)
    while day.isoweekday() > 5:
        day += timedelta(days=1)
    return day


def next_weekday(weekday: int, *, after: int = 3) -> date:
    """The first date with ISO weekday ``weekday`` on or after ``today + after``."""
    day = today() + timedelta(days=after)
    while day.isoweekday() != weekday:
        day += timedelta(days=1)
    return day


class LeaveWorld:
    """An organization with leave enabled, a Sat/Sun weekly off, and helpers around it."""

    def __init__(self, arch: Architecture, db_session: Any, authenticate: Any) -> None:
        self.arch = arch
        self.db = db_session
        self.authenticate = authenticate
        self.hr = arch.client
        self.org_id = arch.organization.id
        self.base = arch.base

    # ---------------------------------------------------------------- organization

    def _settings(self, **values: Any) -> None:
        self.db.execute(
            update(OrganizationSettings)
            .where(OrganizationSettings.organization_id == self.org_id)
            .values(**values)
        )
        self.db.commit()

    def configure(self, **settings: Any) -> None:
        # Schedule endpoints need a module on, so enable leave before creating the pattern.
        self._settings(enable_leave_module=True, leave_tracking_start_date=date(2026, 1, 1))
        pattern = self.hr.post(
            f"{self.base}/weekly-off-patterns",
            json={"pattern_code": "SATSUN", "pattern_name": "Saturday and Sunday", "days": SAT_SUN},
        )
        assert pattern.status_code == 201, pattern.text
        values: dict[str, Any] = {
            "enable_leave_module": True,
            "enable_leave_self_service": True,
            "leave_tracking_start_date": date(2026, 1, 1),
            "default_weekly_off_pattern_id": uuid.UUID(pattern.json()["id"]),
        }
        values.update(settings)
        self._settings(**values)
        enable_configurable_permission(
            self.db,
            organization_id=self.org_id,
            role=Role.EMPLOYEE_SELF,
            permission_code="self.leave",
        )
        self.db.commit()

    # ---------------------------------------------------------------- people

    def employee(self, **overrides: Any) -> dict[str, Any]:
        return self.arch.create_employee(**overrides)

    def set_manager(self, employee_id: str, manager_id: str) -> None:
        self.db.execute(
            update(Employee)
            .where(Employee.id == uuid.UUID(employee_id))
            .values(reporting_manager_employee_id=uuid.UUID(manager_id))
        )
        self.db.commit()

    def client_for(self, employee_id: str | None, role: Role) -> Any:
        user = create_user(
            self.db,
            grants=[(self.org_id, role)],
            employee_id=uuid.UUID(employee_id) if employee_id else None,
        )
        self.db.commit()
        return self.authenticate(user), user

    def self_client(self, employee: dict[str, Any]) -> Any:
        return self.client_for(employee["id"], Role.EMPLOYEE_SELF)[0]

    def manager_client(self, employee: dict[str, Any]) -> Any:
        return self.client_for(employee["id"], Role.DEPARTMENT_MANAGER)[0]

    # ---------------------------------------------------------------- leave configuration

    def leave_type(self, code: str = "CL", **overrides: Any) -> dict[str, Any]:
        body = {
            "leave_type_code": code,
            "leave_type_name": f"{code} leave",
            "paid_classification": "PAID",
        }
        body.update(overrides)
        response = self.hr.post(f"{self.base}/leave-types", json=body)
        assert response.status_code == 201, response.text
        return dict(response.json())

    def policy(
        self,
        leave_type: dict[str, Any],
        *,
        code: str | None = None,
        publish: bool = True,
        **fields: Any,
    ) -> dict[str, Any]:
        version = {
            "effective_from": "2026-01-01",
            "allocation_method": "NONE",
            "approval_chain": "MANAGER_ONLY",
        }
        version.update(fields)
        response = self.hr.post(
            f"{self.base}/leave-policies",
            json={
                "leave_type_id": leave_type["id"],
                "policy_code": code or f"P-{leave_type['leave_type_code']}",
                "policy_name": f"{leave_type['leave_type_code']} policy",
                "version": version,
            },
        )
        assert response.status_code == 201, response.text
        policy = dict(response.json())
        if publish:
            draft = policy["versions"][0]
            published = self.hr.post(
                f"{self.base}/leave-policies/{policy['id']}/versions/{draft['id']}/publish",
                headers=if_match(draft),
            )
            assert published.status_code == 200, published.text
        return policy

    def opening(
        self,
        employee: dict[str, Any],
        leave_type: dict[str, Any],
        days: str,
        year_number: int | None = None,
        **extra: Any,
    ) -> Any:
        year = date(year_number or today().year, 1, 1).isoformat()
        body = {
            "transaction_type": "OPENING_BALANCE",
            "leave_type_id": leave_type["id"],
            "leave_year_start": year,
            "quantity_days": days,
            "effective_date": year,
            "reason": "Migrated opening balance from the previous system",
        }
        body.update(extra)
        return self.hr.post(
            f"{self.base}/employees/{employee['id']}/leave-transactions",
            json=body,
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )

    def run_job(self, job_code: str, as_of: date) -> Any:
        return self.hr.post(
            f"{self.base}/leave-processing/runs",
            json={"job_code": job_code, "as_of_date": as_of.isoformat()},
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )

    def ledger(self, employee: dict[str, Any], year: int | None = None) -> list[dict[str, Any]]:
        query = f"?limit=100&leave_year={year}-01-01" if year else "?limit=100"
        response = self.hr.get(f"{self.base}/employees/{employee['id']}/leave-transactions{query}")
        assert response.status_code == 200, response.text
        return list(response.json()["items"])

    def balance(
        self, employee: dict[str, Any], leave_type: dict[str, Any], year: int | None = None
    ) -> dict[str, Any]:
        query = f"?leave_year={year}-01-01" if year else ""
        response = self.hr.get(f"{self.base}/employees/{employee['id']}/leave-balances{query}")
        assert response.status_code == 200, response.text
        rows = [r for r in response.json()["items"] if r["leave_type_id"] == leave_type["id"]]
        return dict(rows[0]) if rows else {}

    def apply(
        self,
        client: Any,
        leave_type: dict[str, Any],
        start: date,
        end: date | None = None,
        **extra: Any,
    ) -> Any:
        body = {
            "leave_type_id": leave_type["id"],
            "start_date": start.isoformat(),
            "end_date": (end or start).isoformat(),
            "start_half": "FIRST_HALF",
            "end_half": "SECOND_HALF",
        }
        body.update(extra)
        return client.post(
            f"{self.base}/me/leave-applications",
            json=body,
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )


def make_world(db_session: Any, authenticate: Any, **settings: Any) -> LeaveWorld:
    arch = setup_architecture(db_session, authenticate, with_job=False)
    world = LeaveWorld(arch, db_session, authenticate)
    world.configure(**settings)
    return world


def utc_today() -> date:
    return datetime.now(UTC).date()
