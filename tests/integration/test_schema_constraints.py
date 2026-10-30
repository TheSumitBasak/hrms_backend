"""Database-level enforcement of the §21.4 cross-entity constraints.

Failures are asserted as :class:`sqlalchemy.exc.DatabaseError`, the common parent: MySQL
reports a violated CHECK constraint as ``OperationalError`` and a duplicate key or failed
foreign key as ``IntegrityError``. The distinction is a MySQL/driver detail, and what these
tests assert is that the database refused the write. The mapping from error number to the
§24.3 error code is covered separately by ``tests/unit/test_db_error_translation.py``.

Every test here writes with SQLAlchemy Core, **bypassing the service layer entirely**. That is
the point: §18.8 states "application validation alone is insufficient and MUST NOT be the only
safeguard", and AC-03, AC-04, AC-10 and AC-21 each require the database to reject a direct
insert, not merely the API. A test that went through a service would prove nothing about the
schema.

Requires a live MySQL 8 database (§5 of the brief).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any

import pytest
from sqlalchemy import Engine, insert, text, update
from sqlalchemy.exc import DatabaseError

from app.db.base import Base
from app.db.types import uuid7
from app.models import import_all_models

import_all_models()

ORGANIZATIONS = Base.metadata.tables["organizations"]
DEPARTMENTS = Base.metadata.tables["departments"]
GRADES = Base.metadata.tables["grades"]
EMPLOYEES = Base.metadata.tables["employees"]
IDENTIFIERS = Base.metadata.tables["employee_identifiers"]
BANK_ACCOUNTS = Base.metadata.tables["employee_bank_accounts"]
ASSIGNMENTS = Base.metadata.tables["employee_salary_assignments"]
SALARY_COMPONENTS = Base.metadata.tables["salary_components"]
REGISTRATIONS = Base.metadata.tables["organization_registrations"]
CODE_REGISTRY = Base.metadata.tables["employee_code_registry"]
ACCESS = Base.metadata.tables["user_organization_access"]

NOW = datetime(2026, 10, 28, 6, 44, tzinfo=UTC)


# --------------------------------------------------------------------------- row builders


def organization_row(code: str, **overrides: Any) -> dict[str, Any]:
    """A minimal valid organization. §7.2 makes the registered address mandatory."""
    row: dict[str, Any] = {
        "id": uuid7(),
        "organization_code": code,
        "legal_name": f"{code} Technologies Private Limited",
        "display_name": f"{code} Tech",
        "entity_type": "PRIVATE_LIMITED",
        # §7.3: a private limited company requires a CIN and must not carry an LLPIN.
        "cin": f"U72200GJ2015PTC{code[-1]}83456"[:21],
        "registered_address_line_1": "Plot 12, GIDC Electronic Estate",
        "registered_address_city": "Gandhinagar",
        "registered_address_state": "Gujarat",
        "registered_address_state_code": "24",
        "registered_address_country": "IN",
        "registered_address_postal_code": "382025",
        "financial_year_start_month": 4,
        "default_currency": "INR",
        "timezone": "Asia/Kolkata",
        "status": "ACTIVE",
        "permanent_address_same_as_current": False,
    }
    row.pop("permanent_address_same_as_current")
    row.update(overrides)
    return row


def master_row(organization_id: uuid.UUID, code: str, **overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": uuid7(),
        "organization_id": organization_id,
        "code": code,
        "name": f"{code} master",
        "status": "ACTIVE",
    }
    row.update(overrides)
    return row


def employee_row(organization_id: uuid.UUID, **overrides: Any) -> dict[str, Any]:
    """A DRAFT employee, which §10.2 allows with only organization and first name."""
    row: dict[str, Any] = {
        "id": uuid7(),
        "organization_id": organization_id,
        "first_name": "Rajesh",
        "full_name_normalized": "rajesh",
        "status": "DRAFT",
        "work_mode": "ON_SITE",
        "employment_transition_type": "NEW_HIRE",
        "age_exception_status": "NONE",
        "permanent_address_same_as_current": False,
        "international_worker_status": False,
        "legal_hold": False,
    }
    row.update(overrides)
    return row


@pytest.fixture
def execute(migrated_database: Engine) -> Callable[..., Any]:
    """Run statements in a transaction that is always rolled back.

    Each test therefore sees a clean schema and leaves nothing behind, without truncating
    tables between tests.
    """
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
    row = organization_row("ABC")
    execute(insert(ORGANIZATIONS).values(**row))
    return row["id"]


@pytest.fixture
def org_b(execute: Callable[..., Any]) -> uuid.UUID:
    row = organization_row("XYZ")
    execute(insert(ORGANIZATIONS).values(**row))
    return row["id"]


# --------------------------------------------------------------------------- tests


class TestOrganizationConsistency:
    """C-03 and AC-03: the database refuses a cross-organization reference."""

    def test_employee_cannot_reference_another_organizations_department(
        self, execute: Callable[..., Any], org_a: uuid.UUID, org_b: uuid.UUID
    ) -> None:
        """AC-03: "DB composite FK also rejects a direct insert"."""
        foreign_department = master_row(org_b, "IT")
        execute(insert(DEPARTMENTS).values(**foreign_department))

        with pytest.raises(DatabaseError):
            execute(
                insert(EMPLOYEES).values(
                    **employee_row(org_a, department_id=foreign_department["id"])
                )
            )

    def test_employee_can_reference_its_own_organizations_department(
        self, execute: Callable[..., Any], org_a: uuid.UUID
    ) -> None:
        """The positive control: the constraint must not reject legitimate data."""
        own_department = master_row(org_a, "IT")
        execute(insert(DEPARTMENTS).values(**own_department))
        execute(insert(EMPLOYEES).values(**employee_row(org_a, department_id=own_department["id"])))

    def test_manager_must_be_in_the_same_organization(
        self, execute: Callable[..., Any], org_a: uuid.UUID, org_b: uuid.UUID
    ) -> None:
        """C-04."""
        foreign_manager = employee_row(org_b)
        execute(insert(EMPLOYEES).values(**foreign_manager))
        with pytest.raises(DatabaseError):
            execute(
                insert(EMPLOYEES).values(
                    **employee_row(org_a, reporting_manager_employee_id=foreign_manager["id"])
                )
            )

    def test_child_record_cannot_attach_to_another_organizations_employee(
        self, execute: Callable[..., Any], org_a: uuid.UUID, org_b: uuid.UUID
    ) -> None:
        """C-13: a child row's organization must match its employee's."""
        employee = employee_row(org_b)
        execute(insert(EMPLOYEES).values(**employee))
        with pytest.raises(DatabaseError):
            execute(
                insert(IDENTIFIERS).values(
                    id=uuid7(),
                    organization_id=org_a,  # deliberately the wrong organization
                    employee_id=employee["id"],
                    identifier_type="PAN",
                    value_encrypted=b"\x00" * 40,
                    value_blind_index=b"\x01" * 32,
                    value_last_four="234F",
                    key_version=1,
                    is_current=True,
                    verification_status="UNVERIFIED",
                )
            )


class TestEmployeeCodeUniqueness:
    """C-02 and AC-04: an employee code is unique per organization and never reused."""

    def test_duplicate_code_in_same_organization_is_rejected(
        self, execute: Callable[..., Any], org_a: uuid.UUID
    ) -> None:
        execute(insert(EMPLOYEES).values(**employee_row(org_a, employee_code="ABC-0005")))
        with pytest.raises(DatabaseError):
            execute(insert(EMPLOYEES).values(**employee_row(org_a, employee_code="ABC-0005")))

    def test_code_comparison_is_case_insensitive(
        self, execute: Callable[..., Any], org_a: uuid.UUID
    ) -> None:
        """§10.4 keys uniqueness on ``upper(employee_code)``."""
        execute(insert(EMPLOYEES).values(**employee_row(org_a, employee_code="abc-0005")))
        with pytest.raises(DatabaseError):
            execute(insert(EMPLOYEES).values(**employee_row(org_a, employee_code="ABC-0005")))

    def test_same_code_in_a_different_organization_is_allowed(
        self, execute: Callable[..., Any], org_a: uuid.UUID, org_b: uuid.UUID
    ) -> None:
        """AC-02: sequences and codes are per organization."""
        execute(insert(EMPLOYEES).values(**employee_row(org_a, employee_code="EMP-0001")))
        execute(insert(EMPLOYEES).values(**employee_row(org_b, employee_code="EMP-0001")))

    def test_many_drafts_without_a_code_are_allowed(
        self, execute: Callable[..., Any], org_a: uuid.UUID
    ) -> None:
        """§10.4: the code is null in DRAFT when assignment happens on activation.

        NULLs do not collide in the unique index, which is what makes the partial-index
        emulation work (ADR-003).
        """
        for _ in range(3):
            execute(insert(EMPLOYEES).values(**employee_row(org_a, employee_code=None)))

    def test_registry_rejects_a_reused_code(
        self, execute: Callable[..., Any], org_a: uuid.UUID
    ) -> None:
        """§19.7: a retired code stays reserved, so the registry — not ``employees`` — is the
        uniqueness authority."""
        employee = employee_row(org_a, employee_code="ABC-0001")
        execute(insert(EMPLOYEES).values(**employee))
        common = {
            "organization_id": org_a,
            "employee_code_normalized": "ABC-0001",
            "employee_id": employee["id"],
            "source": "MANUAL",
            "status": "ASSIGNED",
        }
        execute(insert(CODE_REGISTRY).values(id=uuid7(), **common))
        with pytest.raises(DatabaseError):
            execute(insert(CODE_REGISTRY).values(id=uuid7(), **common))


class TestOfficialEmailUniqueness:
    """§10.5 / §23.4: unique among non-separated, non-archived records only."""

    def test_duplicate_active_official_email_is_rejected(
        self, execute: Callable[..., Any], org_a: uuid.UUID
    ) -> None:
        execute(
            insert(EMPLOYEES).values(
                **employee_row(org_a, status="ACTIVE", official_email="r.patel@abc.example.in")
            )
        )
        with pytest.raises(DatabaseError):
            execute(
                insert(EMPLOYEES).values(
                    **employee_row(org_a, status="ACTIVE", official_email="r.patel@abc.example.in")
                )
            )

    def test_address_is_reusable_after_separation(
        self, execute: Callable[..., Any], org_a: uuid.UUID
    ) -> None:
        """§23.4 allows reuse "after an employee leaves" — a real operational need."""
        execute(
            insert(EMPLOYEES).values(
                **employee_row(
                    org_a,
                    status="RESIGNED",
                    official_email="r.patel@abc.example.in",
                    employment_start_date=date(2024, 1, 1),
                    employment_end_date=date(2026, 9, 30),
                )
            )
        )
        execute(
            insert(EMPLOYEES).values(
                **employee_row(org_a, status="ACTIVE", official_email="r.patel@abc.example.in")
            )
        )

    def test_case_insensitive(self, execute: Callable[..., Any], org_a: uuid.UUID) -> None:
        execute(
            insert(EMPLOYEES).values(
                **employee_row(org_a, status="ACTIVE", official_email="r.patel@abc.example.in")
            )
        )
        with pytest.raises(DatabaseError):
            execute(
                insert(EMPLOYEES).values(
                    **employee_row(org_a, status="ACTIVE", official_email="R.Patel@ABC.Example.IN")
                )
            )


class TestIdentifierCurrency:
    """C-06: exactly one current identifier per type, except previous PF member IDs."""

    @staticmethod
    def identifier(
        employee_id: uuid.UUID, organization_id: uuid.UUID, **overrides: Any
    ) -> dict[str, Any]:
        row: dict[str, Any] = {
            "id": uuid7(),
            "organization_id": organization_id,
            "employee_id": employee_id,
            "identifier_type": "PAN",
            "value_encrypted": b"\x00" * 40,
            "value_blind_index": uuid.uuid4().bytes * 2,
            "value_last_four": "234F",
            "key_version": 1,
            "is_current": True,
            "verification_status": "UNVERIFIED",
        }
        row.update(overrides)
        return row

    def test_two_current_identifiers_of_the_same_type_are_rejected(
        self, execute: Callable[..., Any], org_a: uuid.UUID
    ) -> None:
        employee = employee_row(org_a)
        execute(insert(EMPLOYEES).values(**employee))
        execute(insert(IDENTIFIERS).values(**self.identifier(employee["id"], org_a)))
        with pytest.raises(DatabaseError):
            execute(insert(IDENTIFIERS).values(**self.identifier(employee["id"], org_a)))

    def test_a_historical_row_may_coexist_with_the_current_one(
        self, execute: Callable[..., Any], org_a: uuid.UUID
    ) -> None:
        """§11.1: changing a value creates a new row and clears ``is_current`` on the old."""
        employee = employee_row(org_a)
        execute(insert(EMPLOYEES).values(**employee))
        execute(
            insert(IDENTIFIERS).values(**self.identifier(employee["id"], org_a, is_current=False))
        )
        execute(insert(IDENTIFIERS).values(**self.identifier(employee["id"], org_a)))

    def test_multiple_current_previous_pf_ids_are_allowed(
        self, execute: Callable[..., Any], org_a: uuid.UUID
    ) -> None:
        """§11.1 names this the one multi-valued identifier type."""
        employee = employee_row(org_a)
        execute(insert(EMPLOYEES).values(**employee))
        for _ in range(3):
            execute(
                insert(IDENTIFIERS).values(
                    **self.identifier(
                        employee["id"], org_a, identifier_type="PREVIOUS_PF_MEMBER_ID"
                    )
                )
            )

    def test_different_types_coexist(self, execute: Callable[..., Any], org_a: uuid.UUID) -> None:
        employee = employee_row(org_a)
        execute(insert(EMPLOYEES).values(**employee))
        for identifier_type in ("PAN", "UAN", "PASSPORT"):
            extra = (
                {"issuing_country": "IN", "expiry_date": date(2030, 1, 1)}
                if identifier_type == "PASSPORT"
                else {}
            )
            execute(
                insert(IDENTIFIERS).values(
                    **self.identifier(
                        employee["id"], org_a, identifier_type=identifier_type, **extra
                    )
                )
            )

    def test_passport_requires_country_and_expiry(
        self, execute: Callable[..., Any], org_a: uuid.UUID
    ) -> None:
        """§11.1/§11.2: both are mandatory for a passport."""
        employee = employee_row(org_a)
        execute(insert(EMPLOYEES).values(**employee))
        with pytest.raises(DatabaseError):
            execute(
                insert(IDENTIFIERS).values(
                    **self.identifier(employee["id"], org_a, identifier_type="PASSPORT")
                )
            )


class TestPrimaryBankAccount:
    """C-07: at most one primary account at a time."""

    @staticmethod
    def account(
        employee_id: uuid.UUID, organization_id: uuid.UUID, **overrides: Any
    ) -> dict[str, Any]:
        row: dict[str, Any] = {
            "id": uuid7(),
            "organization_id": organization_id,
            "employee_id": employee_id,
            "account_holder_name": "Rajesh Kumar Patel",
            "bank_name": "State Bank of India",
            "account_number_encrypted": b"\x00" * 40,
            "account_number_last_four": "6789",
            "account_number_blind_index": uuid.uuid4().bytes * 2,
            "key_version": 1,
            "ifsc_code": "SBIN0001234",
            "account_type": "SAVINGS",
            "is_primary": True,
            "verification_status": "UNVERIFIED",
            "effective_from": date(2026, 11, 2),
        }
        row.update(overrides)
        return row

    def test_two_primaries_starting_the_same_day_are_rejected(
        self, execute: Callable[..., Any], org_a: uuid.UUID
    ) -> None:
        """E-36: the race that must not produce two primary accounts."""
        employee = employee_row(org_a)
        execute(insert(EMPLOYEES).values(**employee))
        execute(insert(BANK_ACCOUNTS).values(**self.account(employee["id"], org_a)))
        with pytest.raises(DatabaseError):
            execute(insert(BANK_ACCOUNTS).values(**self.account(employee["id"], org_a)))

    def test_a_primary_and_a_non_primary_coexist(
        self, execute: Callable[..., Any], org_a: uuid.UUID
    ) -> None:
        employee = employee_row(org_a)
        execute(insert(EMPLOYEES).values(**employee))
        execute(insert(BANK_ACCOUNTS).values(**self.account(employee["id"], org_a)))
        execute(
            insert(BANK_ACCOUNTS).values(**self.account(employee["id"], org_a, is_primary=False))
        )

    def test_verified_requires_proof_document(
        self, execute: Callable[..., Any], org_a: uuid.UUID
    ) -> None:
        """§14.1: a bank account cannot be marked verified without proof."""
        employee = employee_row(org_a)
        execute(insert(EMPLOYEES).values(**employee))
        with pytest.raises(DatabaseError):
            execute(
                insert(BANK_ACCOUNTS).values(
                    **self.account(
                        employee["id"],
                        org_a,
                        verification_status="VERIFIED",
                        verification_date=date(2026, 11, 5),
                    )
                )
            )


class TestSalaryTimeline:
    """C-05 / INV-1 / AC-10: no two confirmed assignments start on the same date."""

    @staticmethod
    def assignment(
        employee_id: uuid.UUID, organization_id: uuid.UUID, **overrides: Any
    ) -> dict[str, Any]:
        row: dict[str, Any] = {
            "id": uuid7(),
            "organization_id": organization_id,
            "employee_id": employee_id,
            "currency": "INR",
            "assignment_status": "ACTIVE",
            "approval_state": "NOT_REQUIRED",
            "effective_from": date(2027, 1, 1),
            "effective_to_source": "NONE",
            "revision_reason": "ANNUAL_REVISION",
            "is_backdated": False,
        }
        row.update(overrides)
        return row

    def test_two_confirmed_assignments_with_the_same_start_are_rejected(
        self, execute: Callable[..., Any], org_a: uuid.UUID
    ) -> None:
        """E-16 / AC-10: "DB exclusion constraint rejects a direct overlapping insert"."""
        employee = employee_row(org_a)
        execute(insert(EMPLOYEES).values(**employee))
        execute(insert(ASSIGNMENTS).values(**self.assignment(employee["id"], org_a)))
        with pytest.raises(DatabaseError):
            execute(
                insert(ASSIGNMENTS).values(
                    **self.assignment(employee["id"], org_a, assignment_status="SCHEDULED")
                )
            )

    def test_drafts_and_cancellations_do_not_occupy_the_timeline(
        self, execute: Callable[..., Any], org_a: uuid.UUID
    ) -> None:
        """§18.1: DRAFT and CANCELLED assignments are not part of the timeline, so several may
        share a start date without conflicting."""
        employee = employee_row(org_a)
        execute(insert(EMPLOYEES).values(**employee))
        execute(
            insert(ASSIGNMENTS).values(
                **self.assignment(employee["id"], org_a, assignment_status="DRAFT")
            )
        )
        execute(
            insert(ASSIGNMENTS).values(
                **self.assignment(employee["id"], org_a, assignment_status="DRAFT")
            )
        )
        execute(
            insert(ASSIGNMENTS).values(
                **self.assignment(
                    employee["id"],
                    org_a,
                    assignment_status="CANCELLED",
                    cancellation_type="USER_CANCEL",
                    cancellation_reason="superseded",
                )
            )
        )
        # ...and a confirmed one on the same date is still allowed, because none of the above
        # claimed the slot.
        execute(insert(ASSIGNMENTS).values(**self.assignment(employee["id"], org_a)))

    def test_adjacent_assignments_are_allowed(
        self, execute: Callable[..., Any], org_a: uuid.UUID
    ) -> None:
        """§18.1: adjacent half-open intervals do not overlap."""
        employee = employee_row(org_a)
        execute(insert(EMPLOYEES).values(**employee))
        execute(
            insert(ASSIGNMENTS).values(
                **self.assignment(
                    employee["id"],
                    org_a,
                    effective_from=date(2026, 4, 1),
                    effective_to=date(2027, 1, 1),
                )
            )
        )
        execute(
            insert(ASSIGNMENTS).values(
                **self.assignment(employee["id"], org_a, effective_from=date(2027, 1, 1))
            )
        )

    def test_cancellation_requires_a_reason(
        self, execute: Callable[..., Any], org_a: uuid.UUID
    ) -> None:
        employee = employee_row(org_a)
        execute(insert(EMPLOYEES).values(**employee))
        with pytest.raises(DatabaseError):
            execute(
                insert(ASSIGNMENTS).values(
                    **self.assignment(employee["id"], org_a, assignment_status="CANCELLED")
                )
            )

    def test_self_approval_is_rejected_by_the_database(
        self, execute: Callable[..., Any], org_a: uuid.UUID, migrated_database: Engine
    ) -> None:
        """§17.4 maker-checker, enforced in the schema as well as the service.

        Self-approval defeats the entire control, so it is not left to one layer.
        """
        employee = employee_row(org_a)
        execute(insert(EMPLOYEES).values(**employee))
        actor = uuid7()
        with pytest.raises(DatabaseError):
            execute(
                insert(ASSIGNMENTS).values(
                    **self.assignment(
                        employee["id"],
                        org_a,
                        created_by=None,
                        submitted_by=actor,
                        approved_by=actor,
                        approved_at=NOW,
                        approval_state="APPROVED",
                    )
                )
            )

    def test_interval_must_be_well_ordered(
        self, execute: Callable[..., Any], org_a: uuid.UUID
    ) -> None:
        """INV-2 / C-14."""
        employee = employee_row(org_a)
        execute(insert(EMPLOYEES).values(**employee))
        with pytest.raises(DatabaseError):
            execute(
                insert(ASSIGNMENTS).values(
                    **self.assignment(
                        employee["id"],
                        org_a,
                        effective_from=date(2027, 6, 1),
                        effective_to=date(2027, 1, 1),
                    )
                )
            )


class TestTypedValueGroup:
    """§16.2: exactly the column matching ``value_type`` is populated."""

    @staticmethod
    def component(organization_id: uuid.UUID, **overrides: Any) -> dict[str, Any]:
        row: dict[str, Any] = {
            "id": uuid7(),
            "organization_id": organization_id,
            "component_code": "BASIC",
            "component_name": "Basic Salary",
            "component_category": "EARNINGS",
            "value_type": "FIXED_AMOUNT",
            "amount_period": "ANNUAL",
            "currency": "INR",
            "is_required": True,
            "is_editable_per_employee": True,
            "display_order": 10,
            "status": "ACTIVE",
        }
        row.update(overrides)
        return row

    def test_fixed_amount_component_accepts_an_amount(
        self, execute: Callable[..., Any], org_a: uuid.UUID
    ) -> None:
        execute(
            insert(SALARY_COMPONENTS).values(**self.component(org_a, default_value_amount=600000))
        )

    def test_fixed_amount_component_rejects_a_percentage(
        self, execute: Callable[..., Any], org_a: uuid.UUID
    ) -> None:
        """A percentage in the money column is exactly the confusion §4.3 guards against."""
        with pytest.raises(DatabaseError):
            execute(
                insert(SALARY_COMPONENTS).values(
                    **self.component(org_a, default_value_percentage=40)
                )
            )

    def test_fixed_amount_requires_a_period(
        self, execute: Callable[..., Any], org_a: uuid.UUID
    ) -> None:
        """§16.3: prevents monthly/annual ambiguity."""
        with pytest.raises(DatabaseError):
            execute(
                insert(SALARY_COMPONENTS).values(
                    **self.component(org_a, amount_period="NOT_APPLICABLE")
                )
            )

    def test_non_amount_component_must_not_claim_a_period(
        self, execute: Callable[..., Any], org_a: uuid.UUID
    ) -> None:
        with pytest.raises(DatabaseError):
            execute(
                insert(SALARY_COMPONENTS).values(
                    **self.component(
                        org_a, value_type="BOOLEAN", amount_period="ANNUAL", currency=None
                    )
                )
            )

    def test_other_configured_requires_a_validation_regex(
        self, execute: Callable[..., Any], org_a: uuid.UUID
    ) -> None:
        with pytest.raises(DatabaseError):
            execute(
                insert(SALARY_COMPONENTS).values(
                    **self.component(
                        org_a,
                        value_type="OTHER_CONFIGURED",
                        amount_period="NOT_APPLICABLE",
                        currency=None,
                    )
                )
            )

    def test_negative_amount_is_rejected(
        self, execute: Callable[..., Any], org_a: uuid.UUID
    ) -> None:
        """C-15: monetary values are non-negative."""
        with pytest.raises(DatabaseError):
            execute(
                insert(SALARY_COMPONENTS).values(**self.component(org_a, default_value_amount=-1))
            )

    def test_percentage_above_one_hundred_is_rejected(
        self, execute: Callable[..., Any], org_a: uuid.UUID
    ) -> None:
        with pytest.raises(DatabaseError):
            execute(
                insert(SALARY_COMPONENTS).values(
                    **self.component(
                        org_a,
                        value_type="PERCENTAGE",
                        amount_period="NOT_APPLICABLE",
                        currency=None,
                        default_value_percentage=101,
                    )
                )
            )


class TestOrganizationIdentifierApplicability:
    """§7.3: which registry identifier an entity type may carry."""

    def test_private_limited_requires_a_cin(self, execute: Callable[..., Any]) -> None:
        with pytest.raises(DatabaseError):
            execute(insert(ORGANIZATIONS).values(**organization_row("NOC", cin=None)))

    def test_private_limited_must_not_carry_an_llpin(self, execute: Callable[..., Any]) -> None:
        with pytest.raises(DatabaseError):
            execute(insert(ORGANIZATIONS).values(**organization_row("BAD", llpin="AAB-1234")))

    def test_llp_requires_an_llpin_and_no_cin(self, execute: Callable[..., Any]) -> None:
        execute(
            insert(ORGANIZATIONS).values(
                **organization_row("LLP", entity_type="LLP", cin=None, llpin="AAB-1234")
            )
        )
        with pytest.raises(DatabaseError):
            execute(
                insert(ORGANIZATIONS).values(
                    **organization_row("LL2", entity_type="LLP", cin=None, llpin=None)
                )
            )

    def test_trust_must_carry_neither(self, execute: Callable[..., Any]) -> None:
        execute(
            insert(ORGANIZATIONS).values(**organization_row("TRS", entity_type="TRUST", cin=None))
        )
        with pytest.raises(DatabaseError):
            execute(insert(ORGANIZATIONS).values(**organization_row("TR2", entity_type="TRUST")))

    def test_pan_is_unique_except_for_proprietorships(self, execute: Callable[..., Any]) -> None:
        """§7.2: a proprietor's PAN may legitimately recur across organizations."""
        execute(insert(ORGANIZATIONS).values(**organization_row("ONE", pan="AABCA1234C")))
        with pytest.raises(DatabaseError):
            execute(insert(ORGANIZATIONS).values(**organization_row("TWO", pan="AABCA1234C")))

    def test_two_proprietorships_may_share_a_pan(self, execute: Callable[..., Any]) -> None:
        for code in ("PR1", "PR2"):
            execute(
                insert(ORGANIZATIONS).values(
                    **organization_row(
                        code, entity_type="PROPRIETORSHIP", cin=None, pan="ABCPE1234F"
                    )
                )
            )

    def test_organization_code_is_unique_case_insensitively(
        self, execute: Callable[..., Any]
    ) -> None:
        execute(insert(ORGANIZATIONS).values(**organization_row("DUP")))
        with pytest.raises(DatabaseError):
            execute(insert(ORGANIZATIONS).values(**organization_row("dup")))


class TestRegistrationUniquenessScopes:
    """§7.4: four different uniqueness scopes, enforced through one generated key."""

    @staticmethod
    def registration(organization_id: uuid.UUID, **overrides: Any) -> dict[str, Any]:
        row: dict[str, Any] = {
            "id": uuid7(),
            "organization_id": organization_id,
            "registration_type": "GSTIN",
            "registration_number": "24AABCA1234C1Z5",
            "state_code": "24",
            "status": "ACTIVE",
        }
        row.update(overrides)
        return row

    def test_gstin_is_unique_across_organizations(
        self, execute: Callable[..., Any], org_a: uuid.UUID, org_b: uuid.UUID
    ) -> None:
        """§7.4: "a GSTIN belongs to one legal entity"."""
        execute(insert(REGISTRATIONS).values(**self.registration(org_a)))
        with pytest.raises(DatabaseError):
            execute(insert(REGISTRATIONS).values(**self.registration(org_b)))

    def test_tan_is_unique_only_within_an_organization(
        self, execute: Callable[..., Any], org_a: uuid.UUID, org_b: uuid.UUID
    ) -> None:
        tan = {"registration_type": "TAN", "registration_number": "AHMA12345B", "state_code": None}
        execute(insert(REGISTRATIONS).values(**self.registration(org_a, **tan)))
        # A different organization may hold the same TAN value.
        execute(insert(REGISTRATIONS).values(**self.registration(org_b, **tan)))
        # The same organization may not.
        with pytest.raises(DatabaseError):
            execute(insert(REGISTRATIONS).values(**self.registration(org_a, **tan)))

    def test_state_scoped_type_requires_a_state_code(
        self, execute: Callable[..., Any], org_a: uuid.UUID
    ) -> None:
        with pytest.raises(DatabaseError):
            execute(
                insert(REGISTRATIONS).values(
                    **self.registration(
                        org_a,
                        registration_type="PT_REGISTRATION",
                        registration_number="PT123456",
                        state_code=None,
                    )
                )
            )

    def test_other_type_requires_a_label(
        self, execute: Callable[..., Any], org_a: uuid.UUID
    ) -> None:
        with pytest.raises(DatabaseError):
            execute(
                insert(REGISTRATIONS).values(
                    **self.registration(
                        org_a,
                        registration_type="OTHER",
                        registration_number="TL-001",
                        state_code=None,
                    )
                )
            )


class TestStandardColumnConstraints:
    """§1.5 technical columns."""

    def test_archiving_requires_a_reason(
        self, execute: Callable[..., Any], org_a: uuid.UUID
    ) -> None:
        employee = employee_row(org_a)
        execute(insert(EMPLOYEES).values(**employee))
        with pytest.raises(DatabaseError):
            execute(
                update(EMPLOYEES)
                .where(EMPLOYEES.c.id == employee["id"])
                .values(archived_at=NOW, archive_reason=None)
            )

    def test_archive_reason_has_a_minimum_length(
        self, execute: Callable[..., Any], org_a: uuid.UUID
    ) -> None:
        """§1.5: 10-500 characters, so "oops" is not an audit trail."""
        employee = employee_row(org_a)
        execute(insert(EMPLOYEES).values(**employee))
        with pytest.raises(DatabaseError):
            execute(
                update(EMPLOYEES)
                .where(EMPLOYEES.c.id == employee["id"])
                .values(archived_at=NOW, archive_reason="oops")
            )

    def test_valid_archival_is_accepted(
        self, execute: Callable[..., Any], org_a: uuid.UUID
    ) -> None:
        employee = employee_row(org_a)
        execute(insert(EMPLOYEES).values(**employee))
        execute(
            update(EMPLOYEES)
            .where(EMPLOYEES.c.id == employee["id"])
            .values(archived_at=NOW, archive_reason="Duplicate record created in error")
        )

    def test_row_version_defaults_to_one(
        self, execute: Callable[..., Any], org_a: uuid.UUID
    ) -> None:
        """§1.5: ``row_version`` starts at 1 and is exposed as the ETag."""
        employee = employee_row(org_a)
        execute(insert(EMPLOYEES).values(**employee))
        result = execute(
            text("SELECT row_version FROM employees WHERE id = :id"), {"id": employee["id"].bytes}
        ).scalar()
        assert result == 1


class TestEmployeeCrossFieldConstraints:
    """§10.7 rules that are expressible as database CHECKs."""

    def test_emp01_end_date_after_start_date(
        self, execute: Callable[..., Any], org_a: uuid.UUID
    ) -> None:
        with pytest.raises(DatabaseError):
            execute(
                insert(EMPLOYEES).values(
                    **employee_row(
                        org_a,
                        status="RESIGNED",
                        employment_start_date=date(2026, 11, 2),
                        employment_end_date=date(2026, 10, 1),
                    )
                )
            )

    def test_separated_status_requires_an_end_date(
        self, execute: Callable[..., Any], org_a: uuid.UUID
    ) -> None:
        """§10.2: a separated employee must have an employment end date."""
        with pytest.raises(DatabaseError):
            execute(insert(EMPLOYEES).values(**employee_row(org_a, status="RESIGNED")))

    def test_on_notice_requires_an_end_date(
        self, execute: Callable[..., Any], org_a: uuid.UUID
    ) -> None:
        with pytest.raises(DatabaseError):
            execute(insert(EMPLOYEES).values(**employee_row(org_a, status="ON_NOTICE")))

    def test_emp05_manager_cannot_be_self(
        self, execute: Callable[..., Any], org_a: uuid.UUID
    ) -> None:
        employee = employee_row(org_a)
        execute(insert(EMPLOYEES).values(**employee))
        with pytest.raises(DatabaseError):
            execute(
                update(EMPLOYEES)
                .where(EMPLOYEES.c.id == employee["id"])
                .values(reporting_manager_employee_id=employee["id"])
            )

    def test_disability_detail_requires_status_yes(
        self, execute: Callable[..., Any], org_a: uuid.UUID
    ) -> None:
        with pytest.raises(DatabaseError):
            execute(
                insert(EMPLOYEES).values(
                    **employee_row(org_a, disability_status="NO", disability_percentage=45)
                )
            )

    def test_rehire_requires_a_previous_record(
        self, execute: Callable[..., Any], org_a: uuid.UUID
    ) -> None:
        """§10.9: a rehire or transfer must name the record it continues from."""
        with pytest.raises(DatabaseError):
            execute(
                insert(EMPLOYEES).values(**employee_row(org_a, employment_transition_type="REHIRE"))
            )

    def test_alternate_mobile_must_differ(
        self, execute: Callable[..., Any], org_a: uuid.UUID
    ) -> None:
        """§10.5."""
        with pytest.raises(DatabaseError):
            execute(
                insert(EMPLOYEES).values(
                    **employee_row(
                        org_a,
                        personal_mobile="+919876543210",
                        alternate_mobile="+919876543210",
                    )
                )
            )


class TestMasterConstraints:
    def test_grade_rank_is_unique_per_organization(
        self, execute: Callable[..., Any], org_a: uuid.UUID, org_b: uuid.UUID
    ) -> None:
        """§9.2. ``rank`` is a MySQL reserved word, so this also exercises the quoting."""
        execute(insert(GRADES).values(**master_row(org_a, "G1", rank=3)))
        with pytest.raises(DatabaseError):
            execute(insert(GRADES).values(**master_row(org_a, "G2", rank=3)))
        # The same rank in another organization is fine.
        execute(insert(GRADES).values(**master_row(org_b, "G1", rank=3)))

    def test_grade_rank_range_is_enforced(
        self, execute: Callable[..., Any], org_a: uuid.UUID
    ) -> None:
        with pytest.raises(DatabaseError):
            execute(insert(GRADES).values(**master_row(org_a, "G9", rank=1000)))

    def test_master_code_is_unique_case_insensitively(
        self, execute: Callable[..., Any], org_a: uuid.UUID
    ) -> None:
        """§9.4: comparison is case-insensitive after trimming."""
        execute(insert(DEPARTMENTS).values(**master_row(org_a, "IT")))
        with pytest.raises(DatabaseError):
            execute(insert(DEPARTMENTS).values(**master_row(org_a, "it")))

    def test_master_code_pattern_is_enforced(
        self, execute: Callable[..., Any], org_a: uuid.UUID
    ) -> None:
        """§21.1 V-CODE."""
        with pytest.raises(DatabaseError):
            execute(insert(DEPARTMENTS).values(**master_row(org_a, "bad code!")))


class TestStrictModeProtectsLengths:
    """Without ``STRICT_ALL_TABLES`` MySQL truncates instead of raising (DEC-041)."""

    def test_over_long_identifier_value_is_rejected(
        self, execute: Callable[..., Any], org_a: uuid.UUID
    ) -> None:
        with pytest.raises(DatabaseError):
            execute(insert(EMPLOYEES).values(**employee_row(org_a, employee_code="X" * 40)))
