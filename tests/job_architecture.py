"""Shared setup for the position / job architecture suites (JA §21).

Kept out of ``conftest.py`` deliberately, like ``tests/factories.py``: a test that needs an
organization with grades, a job and an open position says so by importing
:func:`setup_architecture`, rather than inheriting a large ambient fixture whose contents are
invisible at the call site.

Everything is driven through the real HTTP API, not by inserting rows. A fixture that wrote
positions directly would not exercise the capacity checks, the lifecycle rules or the audit
writes that these suites exist to verify.
"""

from __future__ import annotations

import uuid
from typing import Any

from app.core.permissions import Role
from tests.factories import create_organization, create_user, unique_suffix

BRANCH = {
    "branch_code": "BLR01",
    "branch_name": "Bengaluru Office",
    "address_line_1": "14, Residency Road",
    "city": "Bengaluru",
    "state": "Karnataka",
    "postal_code": "560025",
}


class Architecture:
    """The ids a job-architecture test needs, with a few convenience writers."""

    __slots__ = ("base", "client", "ids", "organization")

    def __init__(self, *, organization: Any, client: Any, ids: dict[str, Any]) -> None:
        self.organization = organization
        self.client = client
        self.ids = ids
        self.base = f"/api/v1/orgs/{organization.id}"

    # ------------------------------------------------------------------ writers

    def create_family(self, **overrides: Any) -> dict[str, Any]:
        body = {
            "family_code": f"FAM{unique_suffix()[:5].upper()}",
            "name": "Engineering",
            "node_level": "FAMILY",
        }
        body.update(overrides)
        response = self.client.post(f"{self.base}/job-families", json=body)
        assert response.status_code == 201, response.text
        return dict(response.json())

    def create_job(self, **overrides: Any) -> dict[str, Any]:
        body = {
            "job_code": f"JOB{unique_suffix()[:5].upper()}",
            "title": "Software Engineer",
            "job_family_id": self.ids["family_id"],
            "default_grade_id": self.ids["grade_3_id"],
        }
        body.update(overrides)
        response = self.client.post(f"{self.base}/job-catalog", json=body)
        assert response.status_code == 201, response.text
        return dict(response.json())

    def create_competency(self, **overrides: Any) -> dict[str, Any]:
        body = {
            "competency_code": f"CMP{unique_suffix()[:5].upper()}",
            "name": "Distributed systems",
            "category_code": "TECHNICAL",
        }
        body.update(overrides)
        response = self.client.post(f"{self.base}/competencies", json=body)
        assert response.status_code == 201, response.text
        return dict(response.json())

    def create_position(self, **overrides: Any) -> dict[str, Any]:
        body = {
            "position_code": f"POS{unique_suffix()[:5].upper()}",
            "job_id": overrides.pop("job_id", None) or self.ids["job_id"],
            "department_id": self.ids["department_id"],
            "authorized_fte": "1.00",
            "max_incumbents": 1,
        }
        body.update(overrides)
        response = self.client.post(f"{self.base}/positions", json=body, headers=_idempotent())
        assert response.status_code == 201, response.text
        return dict(response.json())

    def open_position(self, position: dict[str, Any], **extra: Any) -> dict[str, Any]:
        response = self.client.post(
            f"{self.base}/positions/{position['id']}/open",
            json={"reason": "Authorized by the workforce plan", **extra},
            headers=_if_match(position),
        )
        assert response.status_code == 200, response.text
        return dict(response.json())

    def open_new_position(self, **overrides: Any) -> dict[str, Any]:
        """A position already ``OPEN`` and ready to fill — the common starting point."""
        return self.open_position(self.create_position(**overrides))

    def create_employee(self, *, activate: bool = True, **overrides: Any) -> dict[str, Any]:
        body = {
            "first_name": "Nikhil",
            "last_name": f"Rao{unique_suffix()[:4]}",
            "date_of_birth": "1992-03-11",
            "gender": "MALE",
            "nationality": "IN",
            "personal_mobile": "+919812345678",
            "joining_date": "2026-01-05",
            "employment_type_id": self.ids["employment_type_id"],
            "department_id": self.ids["department_id"],
            "designation_id": self.ids["designation_id"],
            "branch_id": self.ids["branch_id"],
            "current_address": {
                "line_1": "41, Jayanagar 4th Block",
                "city": "Bengaluru",
                "state": "Karnataka",
                "postal_code": "560041",
            },
        }
        body.update(overrides)
        response = self.client.post(f"{self.base}/employees", json=body)
        assert response.status_code == 201, response.text
        employee = response.json()
        if not activate:
            return dict(employee)
        activated = self.client.post(f"{self.base}/employees/{employee['id']}/activate")
        assert activated.status_code == 200, activated.text
        return dict(activated.json())

    def fill(
        self,
        employee: dict[str, Any],
        position: dict[str, Any],
        *,
        fte: str = "1.00",
        assignment_type: str = "PRIMARY",
        effective_date: str = "2026-02-01",
        end_current: bool = True,
        **extra: Any,
    ) -> Any:
        """Assign an employee through the existing job-change endpoint (JA §7.7)."""
        body: dict[str, Any] = {
            "effective_date": effective_date,
            "change_type": "TRANSFER",
            "reason": "Approved move into the position",
            "position_change": {
                "position_id": position["id"],
                "assignment_type": assignment_type,
                "fte": fte,
                "end_current_assignment": end_current,
                "reason": "Approved move",
            },
        }
        body.update(extra)
        return self.client.post(f"{self.base}/employees/{employee['id']}/job-changes", json=body)

    def headcount(self, position: dict[str, Any], **params: Any) -> dict[str, Any]:
        response = self.client.get(
            f"{self.base}/positions/{position['id']}/headcount", params=params
        )
        assert response.status_code == 200, response.text
        return dict(response.json())

    def reload_family(self, family_id: str) -> dict[str, Any]:
        response = self.client.get(f"{self.base}/job-families/{family_id}")
        assert response.status_code == 200, response.text
        return dict(response.json())

    def reload_job(self, job_id: str) -> dict[str, Any]:
        response = self.client.get(f"{self.base}/job-catalog/{job_id}")
        assert response.status_code == 200, response.text
        return dict(response.json())

    def reload_position(self, position_id: str) -> dict[str, Any]:
        response = self.client.get(f"{self.base}/positions/{position_id}")
        assert response.status_code == 200, response.text
        return dict(response.json())

    def create_plan(self, **overrides: Any) -> dict[str, Any]:
        body = {
            "plan_code": f"PLN{unique_suffix()[:5].upper()}",
            "name": "Department establishment",
            "scope_type": "DEPARTMENT",
            "department_id": self.ids["department_id"],
            "authorized_fte": "2.00",
            "authorized_headcount": 2,
            "effective_from": "2026-01-01",
        }
        body.update(overrides)
        response = self.client.post(
            f"{self.base}/headcount-plans", json=body, headers=_idempotent()
        )
        assert response.status_code == 201, response.text
        return dict(response.json())


def setup_architecture(
    db_session: Any,
    authenticate: Any,
    *,
    roles: tuple[Role, ...] = (Role.ORGANIZATION_ADMIN, Role.HR_ADMIN),
    with_job: bool = True,
) -> Architecture:
    """An organization with masters, grades and (by default) one job, ready for positions."""
    organization = create_organization(db_session)
    user = create_user(db_session, grants=[(organization.id, role) for role in roles])
    db_session.commit()
    client = authenticate(user)
    base = f"/api/v1/orgs/{organization.id}"

    branch = client.post(f"{base}/branches", json=BRANCH)
    assert branch.status_code == 201, branch.text
    department = client.post(
        f"{base}/masters/departments", json={"code": "ENGG", "name": "Engineering"}
    )
    assert department.status_code == 201, department.text
    sub_department = client.post(
        f"{base}/masters/sub-departments",
        json={
            "code": "PLATFORM",
            "name": "Platform",
            "department_id": department.json()["id"],
        },
    )
    assert sub_department.status_code == 201, sub_department.text
    designation = client.post(
        f"{base}/masters/designations", json={"code": "SE", "name": "Software Engineer"}
    )
    assert designation.status_code == 201, designation.text

    # Three grades at ranks 1, 3 and 5, so a range test has room on both sides.
    grades = {}
    for rank in (1, 3, 5):
        response = client.post(
            f"{base}/masters/grades",
            json={"code": f"G{rank}", "name": f"Grade {rank}", "rank": rank},
        )
        assert response.status_code == 201, response.text
        grades[rank] = response.json()["id"]

    employment_types = client.get(f"{base}/masters/employment-types").json()
    permanent = next(row for row in employment_types if row["base_type"] == "PERMANENT")

    rule = client.post(
        f"{base}/employee-id-rules",
        json={
            "rule_name": "Organization-wide codes",
            "rule_code": "ORG-SERIES",
            "format_pattern": "{ORG}{SEP}{SEQ}",
            "separator": "-",
            "sequence_reset_policy": "NEVER",
            "sequence_start": 1,
            "sequence_increment": 1,
            "sequence_padding_length": 4,
            "effective_from": "2020-01-01",
            "priority": 100,
        },
    )
    assert rule.status_code == 201, rule.text
    published = client.post(f"{base}/employee-id-rules/{rule.json()['id']}/publish")
    assert published.status_code == 200, published.text

    ids: dict[str, Any] = {
        "branch_id": branch.json()["id"],
        "department_id": department.json()["id"],
        "sub_department_id": sub_department.json()["id"],
        "designation_id": designation.json()["id"],
        "employment_type_id": permanent["id"],
        "grade_1_id": grades[1],
        "grade_3_id": grades[3],
        "grade_5_id": grades[5],
    }
    architecture = Architecture(organization=organization, client=client, ids=ids)
    family = architecture.create_family(family_code="ENG", name="Engineering")
    ids["family_id"] = family["id"]
    if with_job:
        job = architecture.create_job(job_code="SE-001")
        ids["job_id"] = job["id"]
        ids["job"] = job
    return architecture


def assignment_service_for(arch: Architecture) -> tuple[Any, Any]:
    """Build a real :class:`PositionAssignmentService` on its own session.

    JA §18.2 says an onboarding module must write occupancy *through this service boundary*
    rather than inserting rows. There is no onboarding module here, so a test that needs a
    ``SCHEDULED`` assignment stands in for one — and by using the same service the boundary
    prescribes, it exercises the capacity checks and audit writes a real producer would.
    """
    from app.audit.writer import AuditWriter
    from app.db.session import get_session_factory
    from app.repositories.employee import EmployeeRepository
    from app.repositories.master import GradeRepository, MasterReferenceChecker
    from app.repositories.organization import (
        OrganizationRepository,
        OrganizationSettingsRepository,
    )
    from app.repositories.position import (
        HeadcountChangeRequestRepository,
        HeadcountPlanRepository,
        HeadcountReservationRepository,
        JobRepository,
        PositionAssignmentRepository,
        PositionRepository,
        PositionStatusHistoryRepository,
    )
    from app.services.capacity import CapacityCalculator
    from app.services.position_assignments import PositionAssignmentService
    from app.services.positions import PositionService

    session = get_session_factory()()
    organization_id = arch.organization.id
    positions = PositionRepository(session, organization_id)
    assignments = PositionAssignmentRepository(session, organization_id)
    reservations = HeadcountReservationRepository(session, organization_id)
    plans = HeadcountPlanRepository(session, organization_id)
    organizations = OrganizationRepository(session)
    capacity = CapacityCalculator(
        positions=positions, assignments=assignments, reservations=reservations, plans=plans
    )
    position_service = PositionService(
        positions=positions,
        status_history=PositionStatusHistoryRepository(session, organization_id),
        assignments=assignments,
        reservations=reservations,
        requests=HeadcountChangeRequestRepository(session, organization_id),
        jobs=JobRepository(session, organization_id),
        grades=GradeRepository(session, organization_id),
        masters=MasterReferenceChecker(session, organization_id),
        organizations=organizations,
        settings=OrganizationSettingsRepository(session),
        capacity=capacity,
        audit=AuditWriter(session),
    )
    service = PositionAssignmentService(
        assignments=assignments,
        positions=positions,
        employees=EmployeeRepository(session, organization_id),
        organizations=organizations,
        position_service=position_service,
        capacity=capacity,
        audit=AuditWriter(session),
    )
    return service, session


def org_context_for(arch: Architecture, session: Any) -> Any:
    """An ``OrgContext`` for the test's own user, with the permissions a filler needs."""
    from app.core.context import OrgContext, RequestMetadata
    from app.core.permissions import ROLE_PERMISSIONS, PermissionSet, Role
    from app.models.platform import User

    user = (
        session.query(User)
        .filter(User.email.like("user-%@example.test"))
        .order_by(User.created_at.desc())
        .first()
    )
    assert user is not None, "the architecture fixture always creates a user"
    return OrgContext(
        user_id=user.id,
        email=user.email,
        display_name=user.display_name,
        metadata=RequestMetadata(),
        organization_id=arch.organization.id,
        permissions=PermissionSet(
            roles=frozenset({Role.ORGANIZATION_ADMIN, Role.HR_ADMIN}),
            permissions=ROLE_PERMISSIONS[Role.ORGANIZATION_ADMIN] | ROLE_PERMISSIONS[Role.HR_ADMIN],
        ),
    )


def _idempotent() -> dict[str, str]:
    """A fresh ``Idempotency-Key``: required on the JA §13.11 create endpoints."""
    return {"Idempotency-Key": str(uuid.uuid4())}


def _if_match(resource: dict[str, Any]) -> dict[str, str]:
    return {"If-Match": str(resource["row_version"])}


idempotent = _idempotent
"""Exported under a public name: several suites need their own extra idempotent create."""

if_match = _if_match

__all__ = [
    "BRANCH",
    "Architecture",
    "assignment_service_for",
    "idempotent",
    "if_match",
    "org_context_for",
    "setup_architecture",
]
