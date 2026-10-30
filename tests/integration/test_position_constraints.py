"""Database-level enforcement of the JA §10 constraints (JA §21.5).

Every write here uses SQLAlchemy Core and **bypasses the service layer entirely**. That is the
point: §18.8 says application validation "MUST NOT be the only safeguard", and JA §21.5 asks
specifically for direct inserts to be rejected. A test that went through a service would prove
nothing about the schema.

Failures are asserted as :class:`DatabaseError`, the common parent — MySQL reports a violated
CHECK as ``OperationalError`` and a duplicate key or failed foreign key as ``IntegrityError``,
and which one arrives is a driver detail. What matters is that the database refused.

Requires a live MySQL 8 database (§5 of the brief).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any

import pytest
from sqlalchemy import Engine, insert
from sqlalchemy.exc import DatabaseError

from app.db.base import Base
from app.db.types import uuid7
from app.models import import_all_models
from tests.integration.test_schema_constraints import (
    employee_row,
    master_row,
    organization_row,
)

import_all_models()

ORGANIZATIONS = Base.metadata.tables["organizations"]
DEPARTMENTS = Base.metadata.tables["departments"]
GRADES = Base.metadata.tables["grades"]
EMPLOYEES = Base.metadata.tables["employees"]
JOB_FAMILIES = Base.metadata.tables["job_families"]
JOBS = Base.metadata.tables["jobs"]
COMPETENCIES = Base.metadata.tables["competencies"]
JOB_COMPETENCIES = Base.metadata.tables["job_competencies"]
POSITIONS = Base.metadata.tables["positions"]
ASSIGNMENTS = Base.metadata.tables["employee_position_assignments"]
PLANS = Base.metadata.tables["headcount_plans"]
REQUESTS = Base.metadata.tables["headcount_change_requests"]
RESERVATIONS = Base.metadata.tables["headcount_reservations"]
STATUS_HISTORY = Base.metadata.tables["position_status_history"]

NOW = datetime(2026, 10, 29, 6, 0, tzinfo=UTC)
TODAY = date(2026, 10, 29)


# --------------------------------------------------------------------------- row builders


def family_row(organization_id: uuid.UUID, code: str, **overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": uuid7(),
        "organization_id": organization_id,
        "family_code": code,
        "name": f"{code} family",
        "node_level": "FAMILY",
        "status": "ACTIVE",
    }
    row.update(overrides)
    return row


def job_row(
    organization_id: uuid.UUID, family_id: uuid.UUID, code: str, **overrides: Any
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": uuid7(),
        "organization_id": organization_id,
        "job_code": code,
        "title": f"{code} role",
        "job_family_id": family_id,
        "status": "ACTIVE",
        "effective_from": TODAY,
    }
    row.update(overrides)
    return row


def position_row(
    organization_id: uuid.UUID,
    job_id: uuid.UUID,
    department_id: uuid.UUID,
    code: str,
    **overrides: Any,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": uuid7(),
        "organization_id": organization_id,
        "position_code": code,
        "position_title": f"{code} seat",
        "job_id": job_id,
        "department_id": department_id,
        "work_mode": "ON_SITE",
        "authorized_fte": "1.00",
        "max_incumbents": 1,
        "position_status": "DRAFT",
    }
    row.update(overrides)
    return row


def assignment_row(
    organization_id: uuid.UUID, employee_id: uuid.UUID, position_id: uuid.UUID, **overrides: Any
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": uuid7(),
        "organization_id": organization_id,
        "employee_id": employee_id,
        "position_id": position_id,
        "assignment_type": "PRIMARY",
        "assignment_status": "ACTIVE",
        "fte": "1.00",
        "effective_from": TODAY,
        "source": "MANUAL",
        "active_primary_key": employee_id,
    }
    row.update(overrides)
    return row


def plan_row(organization_id: uuid.UUID, code: str, **overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": uuid7(),
        "organization_id": organization_id,
        "plan_code": code,
        "name": f"{code} plan",
        "scope_type": "ORGANIZATION",
        "authorized_fte": "5.00",
        "authorized_headcount": 5,
        "plan_status": "DRAFT",
        "effective_from": TODAY,
    }
    row.update(overrides)
    return row


def request_row(organization_id: uuid.UUID, **overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": uuid7(),
        "organization_id": organization_id,
        "change_type": "INCREASE",
        "current_fte": "1.00",
        "requested_fte": "2.00",
        "current_headcount": 1,
        "requested_headcount": 2,
        "reason": "A reason of at least twenty characters for the record",
        "request_status": "DRAFT",
    }
    row.update(overrides)
    return row


# --------------------------------------------------------------------------- fixtures


@pytest.fixture
def execute(migrated_database: Engine) -> Callable[..., Any]:
    """Run statements in a transaction that is always rolled back."""
    connection = migrated_database.connect()
    transaction = connection.begin()

    def run(statement: Any, parameters: Any = None) -> Any:
        return (
            connection.execute(statement, parameters)
            if parameters
            else connection.execute(statement)
        )

    try:
        yield run
    finally:
        transaction.rollback()
        connection.close()


@pytest.fixture
def org_a(execute: Callable[..., Any]) -> uuid.UUID:
    row = organization_row("JAA")
    execute(insert(ORGANIZATIONS).values(**row))
    return row["id"]


@pytest.fixture
def org_b(execute: Callable[..., Any]) -> uuid.UUID:
    row = organization_row("JAB")
    execute(insert(ORGANIZATIONS).values(**row))
    return row["id"]


@pytest.fixture
def scaffold(execute: Callable[..., Any], org_a: uuid.UUID) -> dict[str, Any]:
    """A family, a job and a department in organization A."""
    family = family_row(org_a, "ENG")
    department = master_row(org_a, "ENGG")
    execute(insert(JOB_FAMILIES).values(**family))
    execute(insert(DEPARTMENTS).values(**department))
    job = job_row(org_a, family["id"], "SE")
    execute(insert(JOBS).values(**job))
    return {
        "organization_id": org_a,
        "family_id": family["id"],
        "department_id": department["id"],
        "job_id": job["id"],
    }


# --------------------------------------------------------------------------- tests


class TestCodeUniqueness:
    """JA §11.3: codes are unique per organization, case-insensitively."""

    def test_duplicate_job_code_is_rejected_case_insensitively(
        self, execute: Callable[..., Any], scaffold: dict[str, Any]
    ) -> None:
        with pytest.raises(DatabaseError):
            execute(
                insert(JOBS).values(
                    **job_row(scaffold["organization_id"], scaffold["family_id"], "se")
                )
            )

    def test_the_same_job_code_in_another_organization_is_accepted(
        self,
        execute: Callable[..., Any],
        scaffold: dict[str, Any],
        org_b: uuid.UUID,
    ) -> None:
        """The positive control: uniqueness is per tenant, not global."""
        other_family = family_row(org_b, "ENG")
        execute(insert(JOB_FAMILIES).values(**other_family))
        execute(insert(JOBS).values(**job_row(org_b, other_family["id"], "SE")))

    def test_duplicate_family_competency_and_position_codes_are_rejected(
        self, execute: Callable[..., Any], scaffold: dict[str, Any]
    ) -> None:
        organization_id = scaffold["organization_id"]
        with pytest.raises(DatabaseError):
            execute(insert(JOB_FAMILIES).values(**family_row(organization_id, "eng")))

        competency = {
            "id": uuid7(),
            "organization_id": organization_id,
            "competency_code": "DS",
            "name": "Distributed systems",
            "category_code": "TECHNICAL",
            "status": "ACTIVE",
        }
        execute(insert(COMPETENCIES).values(**competency))
        with pytest.raises(DatabaseError):
            execute(
                insert(COMPETENCIES).values(
                    **{**competency, "id": uuid7(), "competency_code": "ds"}
                )
            )

        position = position_row(
            organization_id, scaffold["job_id"], scaffold["department_id"], "P1"
        )
        execute(insert(POSITIONS).values(**position))
        with pytest.raises(DatabaseError):
            execute(
                insert(POSITIONS).values(
                    **position_row(
                        organization_id, scaffold["job_id"], scaffold["department_id"], "p1"
                    )
                )
            )

    def test_duplicate_plan_code_is_rejected(
        self, execute: Callable[..., Any], org_a: uuid.UUID
    ) -> None:
        execute(insert(PLANS).values(**plan_row(org_a, "PLAN1")))
        with pytest.raises(DatabaseError):
            execute(insert(PLANS).values(**plan_row(org_a, "plan1")))


class TestCrossOrganizationReferences:
    """AC-JA-02: the composite foreign keys refuse a direct cross-tenant insert."""

    def test_a_job_cannot_reference_another_organizations_family(
        self, execute: Callable[..., Any], org_a: uuid.UUID, org_b: uuid.UUID
    ) -> None:
        foreign = family_row(org_b, "FOREIGN")
        execute(insert(JOB_FAMILIES).values(**foreign))
        with pytest.raises(DatabaseError):
            execute(insert(JOBS).values(**job_row(org_a, foreign["id"], "X1")))

    def test_a_job_cannot_reference_another_organizations_grade(
        self,
        execute: Callable[..., Any],
        scaffold: dict[str, Any],
        org_b: uuid.UUID,
    ) -> None:
        foreign_grade = master_row(org_b, "G9", rank=9)
        execute(insert(GRADES).values(**foreign_grade))
        with pytest.raises(DatabaseError):
            execute(
                insert(JOBS).values(
                    **job_row(
                        scaffold["organization_id"],
                        scaffold["family_id"],
                        "X2",
                        default_grade_id=foreign_grade["id"],
                    )
                )
            )

    def test_a_position_cannot_reference_another_organizations_department(
        self,
        execute: Callable[..., Any],
        scaffold: dict[str, Any],
        org_b: uuid.UUID,
    ) -> None:
        foreign_department = master_row(org_b, "SALES")
        execute(insert(DEPARTMENTS).values(**foreign_department))
        with pytest.raises(DatabaseError):
            execute(
                insert(POSITIONS).values(
                    **position_row(
                        scaffold["organization_id"],
                        scaffold["job_id"],
                        foreign_department["id"],
                        "X3",
                    )
                )
            )

    def test_an_assignment_cannot_cross_organizations(
        self,
        execute: Callable[..., Any],
        scaffold: dict[str, Any],
        org_b: uuid.UUID,
    ) -> None:
        position = position_row(
            scaffold["organization_id"],
            scaffold["job_id"],
            scaffold["department_id"],
            "P-ASSIGN",
            position_status="OPEN",
            opened_at=NOW,
        )
        execute(insert(POSITIONS).values(**position))
        foreign_employee = employee_row(org_b)
        execute(insert(EMPLOYEES).values(**foreign_employee))
        with pytest.raises(DatabaseError):
            execute(
                insert(ASSIGNMENTS).values(
                    **assignment_row(
                        scaffold["organization_id"], foreign_employee["id"], position["id"]
                    )
                )
            )


class TestJobFamilyShape:
    """JA §10.3: the two-level hierarchy is partly a database guarantee."""

    def test_a_family_cannot_be_its_own_parent(
        self, execute: Callable[..., Any], org_a: uuid.UUID
    ) -> None:
        row = family_row(org_a, "SELFP", node_level="SUBFAMILY")
        row["parent_job_family_id"] = row["id"]
        with pytest.raises(DatabaseError):
            execute(insert(JOB_FAMILIES).values(**row))

    def test_a_family_must_not_have_a_parent(
        self, execute: Callable[..., Any], org_a: uuid.UUID
    ) -> None:
        parent = family_row(org_a, "PARENT")
        execute(insert(JOB_FAMILIES).values(**parent))
        with pytest.raises(DatabaseError):
            execute(
                insert(JOB_FAMILIES).values(
                    **family_row(
                        org_a, "CHILD", node_level="FAMILY", parent_job_family_id=parent["id"]
                    )
                )
            )

    def test_a_subfamily_must_have_a_parent(
        self, execute: Callable[..., Any], org_a: uuid.UUID
    ) -> None:
        with pytest.raises(DatabaseError):
            execute(
                insert(JOB_FAMILIES).values(**family_row(org_a, "ORPHAN", node_level="SUBFAMILY"))
            )

    def test_an_unknown_node_level_is_rejected(
        self, execute: Callable[..., Any], org_a: uuid.UUID
    ) -> None:
        with pytest.raises(DatabaseError):
            execute(insert(JOB_FAMILIES).values(**family_row(org_a, "WEIRD", node_level="TEAM")))


class TestPositionConstraints:
    def test_a_position_cannot_report_to_itself(
        self, execute: Callable[..., Any], scaffold: dict[str, Any]
    ) -> None:
        """AC-JA-07: the self-reference is refused by the schema, not only the service."""
        row = position_row(
            scaffold["organization_id"], scaffold["job_id"], scaffold["department_id"], "SELF"
        )
        row["reports_to_position_id"] = row["id"]
        with pytest.raises(DatabaseError):
            execute(insert(POSITIONS).values(**row))

    def test_non_positive_fte_and_incumbents_are_rejected(
        self, execute: Callable[..., Any], scaffold: dict[str, Any]
    ) -> None:
        for field, value in (
            ("authorized_fte", "0.00"),
            ("authorized_fte", "-1.00"),
            ("max_incumbents", 0),
        ):
            with pytest.raises(DatabaseError):
                execute(
                    insert(POSITIONS).values(
                        **position_row(
                            scaffold["organization_id"],
                            scaffold["job_id"],
                            scaffold["department_id"],
                            f"BAD-{field}-{value}",
                            **{field: value},
                        )
                    )
                )

    def test_an_open_position_must_record_when_it_opened(
        self, execute: Callable[..., Any], scaffold: dict[str, Any]
    ) -> None:
        """JA §7.2: the status and the timestamp it implies cannot be separated."""
        with pytest.raises(DatabaseError):
            execute(
                insert(POSITIONS).values(
                    **position_row(
                        scaffold["organization_id"],
                        scaffold["job_id"],
                        scaffold["department_id"],
                        "NO-OPENED-AT",
                        position_status="OPEN",
                    )
                )
            )

    def test_a_frozen_position_must_record_a_reason(
        self, execute: Callable[..., Any], scaffold: dict[str, Any]
    ) -> None:
        with pytest.raises(DatabaseError):
            execute(
                insert(POSITIONS).values(
                    **position_row(
                        scaffold["organization_id"],
                        scaffold["job_id"],
                        scaffold["department_id"],
                        "NO-FREEZE-REASON",
                        position_status="FROZEN",
                        opened_at=NOW,
                    )
                )
            )

    def test_a_closed_position_must_record_a_reason_and_a_time(
        self, execute: Callable[..., Any], scaffold: dict[str, Any]
    ) -> None:
        with pytest.raises(DatabaseError):
            execute(
                insert(POSITIONS).values(
                    **position_row(
                        scaffold["organization_id"],
                        scaffold["job_id"],
                        scaffold["department_id"],
                        "NO-CLOSE-REASON",
                        position_status="CLOSED",
                        opened_at=NOW,
                        closed_at=NOW,
                    )
                )
            )

    def test_planned_dates_must_be_ordered(
        self, execute: Callable[..., Any], scaffold: dict[str, Any]
    ) -> None:
        with pytest.raises(DatabaseError):
            execute(
                insert(POSITIONS).values(
                    **position_row(
                        scaffold["organization_id"],
                        scaffold["job_id"],
                        scaffold["department_id"],
                        "BAD-DATES",
                        planned_start_date=date(2026, 6, 1),
                        planned_end_date=date(2026, 1, 1),
                    )
                )
            )

    def test_archiving_without_a_reason_is_rejected(
        self, execute: Callable[..., Any], scaffold: dict[str, Any]
    ) -> None:
        """§1.5: the archive reason is mandatory and at least ten characters."""
        for reason in (None, "short"):
            with pytest.raises(DatabaseError):
                execute(
                    insert(POSITIONS).values(
                        **position_row(
                            scaffold["organization_id"],
                            scaffold["job_id"],
                            scaffold["department_id"],
                            f"ARCH-{reason}",
                            archived_at=NOW,
                            archive_reason=reason,
                        )
                    )
                )


class TestAssignmentConstraints:
    @pytest.fixture
    def open_position(
        self, execute: Callable[..., Any], scaffold: dict[str, Any]
    ) -> dict[str, Any]:
        row = position_row(
            scaffold["organization_id"],
            scaffold["job_id"],
            scaffold["department_id"],
            "OPEN-1",
            position_status="OPEN",
            opened_at=NOW,
        )
        execute(insert(POSITIONS).values(**row))
        return row

    @pytest.fixture
    def employee(self, execute: Callable[..., Any], org_a: uuid.UUID) -> dict[str, Any]:
        row = employee_row(org_a)
        execute(insert(EMPLOYEES).values(**row))
        return row

    def test_only_one_active_primary_assignment_per_employee(
        self,
        execute: Callable[..., Any],
        scaffold: dict[str, Any],
        open_position: dict[str, Any],
        employee: dict[str, Any],
    ) -> None:
        """AC-JA-12: the nullable ``active_primary_key`` unique index is the backstop."""
        second_position = position_row(
            scaffold["organization_id"],
            scaffold["job_id"],
            scaffold["department_id"],
            "OPEN-2",
            position_status="OPEN",
            opened_at=NOW,
        )
        execute(insert(POSITIONS).values(**second_position))
        execute(
            insert(ASSIGNMENTS).values(
                **assignment_row(scaffold["organization_id"], employee["id"], open_position["id"])
            )
        )
        with pytest.raises(DatabaseError):
            execute(
                insert(ASSIGNMENTS).values(
                    **assignment_row(
                        scaffold["organization_id"], employee["id"], second_position["id"]
                    )
                )
            )

    def test_an_acting_assignment_may_coexist(
        self,
        execute: Callable[..., Any],
        scaffold: dict[str, Any],
        open_position: dict[str, Any],
        employee: dict[str, Any],
    ) -> None:
        """The positive control: only ``PRIMARY`` is restricted to one active row."""
        second_position = position_row(
            scaffold["organization_id"],
            scaffold["job_id"],
            scaffold["department_id"],
            "OPEN-3",
            position_status="OPEN",
            opened_at=NOW,
        )
        execute(insert(POSITIONS).values(**second_position))
        execute(
            insert(ASSIGNMENTS).values(
                **assignment_row(scaffold["organization_id"], employee["id"], open_position["id"])
            )
        )
        execute(
            insert(ASSIGNMENTS).values(
                **assignment_row(
                    scaffold["organization_id"],
                    employee["id"],
                    second_position["id"],
                    assignment_type="ACTING",
                    active_primary_key=None,
                )
            )
        )

    def test_the_primary_key_column_must_agree_with_the_status(
        self,
        execute: Callable[..., Any],
        scaffold: dict[str, Any],
        open_position: dict[str, Any],
        employee: dict[str, Any],
    ) -> None:
        """A stale key on an ended row would block the employee's next assignment."""
        with pytest.raises(DatabaseError):
            execute(
                insert(ASSIGNMENTS).values(
                    **assignment_row(
                        scaffold["organization_id"],
                        employee["id"],
                        open_position["id"],
                        assignment_status="ENDED",
                        effective_to=date(2026, 12, 1),
                        end_reason="Ended",
                        active_primary_key=employee["id"],
                    )
                )
            )

    def test_an_ended_assignment_needs_an_end_date_and_reason(
        self,
        execute: Callable[..., Any],
        scaffold: dict[str, Any],
        open_position: dict[str, Any],
        employee: dict[str, Any],
    ) -> None:
        with pytest.raises(DatabaseError):
            execute(
                insert(ASSIGNMENTS).values(
                    **assignment_row(
                        scaffold["organization_id"],
                        employee["id"],
                        open_position["id"],
                        assignment_status="ENDED",
                        active_primary_key=None,
                    )
                )
            )

    def test_fte_outside_the_permitted_range_is_rejected(
        self,
        execute: Callable[..., Any],
        scaffold: dict[str, Any],
        open_position: dict[str, Any],
        employee: dict[str, Any],
    ) -> None:
        """JA-HC-008: an assignment is a fraction of one role."""
        for fte in ("0.00", "1.50"):
            with pytest.raises(DatabaseError):
                execute(
                    insert(ASSIGNMENTS).values(
                        **assignment_row(
                            scaffold["organization_id"],
                            employee["id"],
                            open_position["id"],
                            fte=fte,
                        )
                    )
                )

    def test_a_reversed_interval_is_rejected(
        self,
        execute: Callable[..., Any],
        scaffold: dict[str, Any],
        open_position: dict[str, Any],
        employee: dict[str, Any],
    ) -> None:
        with pytest.raises(DatabaseError):
            execute(
                insert(ASSIGNMENTS).values(
                    **assignment_row(
                        scaffold["organization_id"],
                        employee["id"],
                        open_position["id"],
                        effective_from=date(2026, 6, 1),
                        effective_to=date(2026, 1, 1),
                        assignment_status="ENDED",
                        end_reason="Reversed",
                        active_primary_key=None,
                    )
                )
            )

    def test_the_same_employee_position_and_start_date_cannot_repeat(
        self,
        execute: Callable[..., Any],
        scaffold: dict[str, Any],
        open_position: dict[str, Any],
        employee: dict[str, Any],
    ) -> None:
        execute(
            insert(ASSIGNMENTS).values(
                **assignment_row(scaffold["organization_id"], employee["id"], open_position["id"])
            )
        )
        with pytest.raises(DatabaseError):
            execute(
                insert(ASSIGNMENTS).values(
                    **assignment_row(
                        scaffold["organization_id"],
                        employee["id"],
                        open_position["id"],
                        assignment_status="SCHEDULED",
                        active_primary_key=None,
                    )
                )
            )


class TestHeadcountConstraints:
    def test_an_active_plan_requires_an_approver(
        self, execute: Callable[..., Any], org_a: uuid.UUID
    ) -> None:
        """JA §15.4: approval cannot be implied, so the schema insists on the columns."""
        with pytest.raises(DatabaseError):
            execute(insert(PLANS).values(**plan_row(org_a, "NOAPPR", plan_status="ACTIVE")))

    def test_a_plan_needs_exactly_one_scope(
        self, execute: Callable[..., Any], org_a: uuid.UUID, scaffold: dict[str, Any]
    ) -> None:
        missing = plan_row(org_a, "NOSCOPE", scope_type="DEPARTMENT")
        with pytest.raises(DatabaseError):
            execute(insert(PLANS).values(**missing))

        extra = plan_row(
            org_a,
            "TWOSCOPE",
            scope_type="ORGANIZATION",
            department_id=scaffold["department_id"],
        )
        with pytest.raises(DatabaseError):
            execute(insert(PLANS).values(**extra))

    def test_negative_plan_capacity_is_rejected(
        self, execute: Callable[..., Any], org_a: uuid.UUID
    ) -> None:
        with pytest.raises(DatabaseError):
            execute(insert(PLANS).values(**plan_row(org_a, "NEG", authorized_fte="-1.00")))

    def test_a_request_needs_exactly_one_target(
        self, execute: Callable[..., Any], org_a: uuid.UUID, scaffold: dict[str, Any]
    ) -> None:
        """JA §10.11: zero or two targets are both refused."""
        with pytest.raises(DatabaseError):
            execute(insert(REQUESTS).values(**request_row(org_a)))

        position = position_row(
            scaffold["organization_id"], scaffold["job_id"], scaffold["department_id"], "REQ-P"
        )
        execute(insert(POSITIONS).values(**position))
        plan = plan_row(org_a, "REQ-PLAN")
        execute(insert(PLANS).values(**plan))
        with pytest.raises(DatabaseError):
            execute(
                insert(REQUESTS).values(
                    **request_row(org_a, position_id=position["id"], headcount_plan_id=plan["id"])
                )
            )

    def test_a_short_reason_is_rejected(
        self, execute: Callable[..., Any], org_a: uuid.UUID, scaffold: dict[str, Any]
    ) -> None:
        position = position_row(
            scaffold["organization_id"], scaffold["job_id"], scaffold["department_id"], "REQ-S"
        )
        execute(insert(POSITIONS).values(**position))
        with pytest.raises(DatabaseError):
            execute(
                insert(REQUESTS).values(
                    **request_row(org_a, position_id=position["id"], reason="too short")
                )
            )

    def test_a_rejected_request_needs_a_decision_note(
        self, execute: Callable[..., Any], org_a: uuid.UUID, scaffold: dict[str, Any]
    ) -> None:
        position = position_row(
            scaffold["organization_id"], scaffold["job_id"], scaffold["department_id"], "REQ-R"
        )
        execute(insert(POSITIONS).values(**position))
        with pytest.raises(DatabaseError):
            execute(
                insert(REQUESTS).values(
                    **request_row(
                        org_a,
                        position_id=position["id"],
                        request_status="REJECTED",
                        decided_by=None,
                        decided_at=NOW,
                    )
                )
            )

    def test_an_active_reservation_requires_an_expiry(
        self, execute: Callable[..., Any], scaffold: dict[str, Any]
    ) -> None:
        """JA §8.8: an open-ended reservation would hold capacity for ever."""
        position = position_row(
            scaffold["organization_id"],
            scaffold["job_id"],
            scaffold["department_id"],
            "RES-1",
            position_status="OPEN",
            opened_at=NOW,
        )
        execute(insert(POSITIONS).values(**position))
        with pytest.raises(DatabaseError):
            execute(
                insert(RESERVATIONS).values(
                    id=uuid7(),
                    organization_id=scaffold["organization_id"],
                    position_id=position["id"],
                    source_type="OFFER",
                    reserved_fte="1.00",
                    reserved_headcount=1,
                    reservation_status="ACTIVE",
                    effective_from=TODAY,
                    expires_at=None,
                )
            )


class TestCompetencyMappingConstraints:
    def test_proficiency_outside_one_to_five_is_rejected(
        self, execute: Callable[..., Any], scaffold: dict[str, Any]
    ) -> None:
        competency = {
            "id": uuid7(),
            "organization_id": scaffold["organization_id"],
            "competency_code": "PROF",
            "name": "Proficiency bounds",
            "category_code": "TECHNICAL",
            "status": "ACTIVE",
        }
        execute(insert(COMPETENCIES).values(**competency))
        for proficiency in (0, 6):
            with pytest.raises(DatabaseError):
                execute(
                    insert(JOB_COMPETENCIES).values(
                        id=uuid7(),
                        organization_id=scaffold["organization_id"],
                        job_id=scaffold["job_id"],
                        competency_id=competency["id"],
                        requirement_type="REQUIRED",
                        min_proficiency=proficiency,
                        effective_from=TODAY,
                    )
                )

    def test_a_zero_length_interval_is_rejected(
        self, execute: Callable[..., Any], scaffold: dict[str, Any]
    ) -> None:
        """§10.15: a half-open interval must contain at least one date."""
        competency = {
            "id": uuid7(),
            "organization_id": scaffold["organization_id"],
            "competency_code": "ZERO",
            "name": "Zero length",
            "category_code": "TECHNICAL",
            "status": "ACTIVE",
        }
        execute(insert(COMPETENCIES).values(**competency))
        with pytest.raises(DatabaseError):
            execute(
                insert(JOB_COMPETENCIES).values(
                    id=uuid7(),
                    organization_id=scaffold["organization_id"],
                    job_id=scaffold["job_id"],
                    competency_id=competency["id"],
                    requirement_type="REQUIRED",
                    min_proficiency=3,
                    effective_from=TODAY,
                    effective_to=TODAY,
                )
            )


class TestStatusHistoryIsAppendOnly:
    def test_the_table_has_no_update_machinery(self) -> None:
        """§1.5: an append-only table carries no ``row_version`` and no ``updated_*``."""
        columns = set(STATUS_HISTORY.columns.keys())
        assert "row_version" not in columns
        assert "updated_at" not in columns
        assert "archived_at" not in columns
        assert {"id", "created_at", "created_by"} <= columns

    def test_an_unknown_status_is_rejected(
        self, execute: Callable[..., Any], scaffold: dict[str, Any]
    ) -> None:
        position = position_row(
            scaffold["organization_id"], scaffold["job_id"], scaffold["department_id"], "HIST"
        )
        execute(insert(POSITIONS).values(**position))
        with pytest.raises(DatabaseError):
            execute(
                insert(STATUS_HISTORY).values(
                    id=uuid7(),
                    organization_id=scaffold["organization_id"],
                    position_id=position["id"],
                    from_status="DRAFT",
                    to_status="PENDING",
                    effective_at=NOW,
                )
            )
