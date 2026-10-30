"""Schema-shape invariants that hold without a database.

These check the *metadata*, so they run everywhere and fail fast. Three of them encode rules
that are easy to violate accidentally while adding a table:

* every organization-scoped table must be a valid composite-FK target (§23.1);
* append-only tables must have no update machinery (§1.5, §20.1);
* raw constraint SQL must quote MySQL reserved words, or the migration dies at ``CREATE
  TABLE`` with a syntax error that names nothing useful.
"""

from __future__ import annotations

import re
from typing import ClassVar

import pytest
from sqlalchemy import CheckConstraint, Table, UniqueConstraint
from sqlalchemy.dialects.mysql.base import RESERVED_WORDS_MYSQL

from app.db.base import Base
from app.db.types import Uuid7
from app.models import MODEL_MODULES, import_all_models

pytestmark = pytest.mark.unit

import_all_models()

ALL_TABLES: list[Table] = sorted(Base.metadata.tables.values(), key=lambda table: table.name)

APPEND_ONLY_TABLES = {
    "audit_logs",
    "employee_job_history",
    "employee_status_history",
    "salary_revision_history",
    "employee_code_registry",
    "login_attempts",
}
"""§1.5: "Append-only tables (audit logs, history/event tables, code registry) have only id,
created_at, created_by and are never updated"."""

PLATFORM_TABLES = {
    "states",
    "users",
    "roles",
    "permissions",
    "role_permissions",
    "refresh_tokens",
    "idempotency_keys",
    "login_attempts",
    "organizations",
    "organization_groups",
    "identifier_type_configs",
    "integrity_findings",
    "user_organization_access",
    "audit_logs",
    "document_category_mime_types",
    "employee_previous_employment_documents",
    "employee_salary_components",
    "salary_template_components",
    "organization_settings",
    "employee_id_sequences",
}
"""Tables that are not organization-scoped, or are scoped through a parent rather than by
carrying ``organization_id`` themselves."""


class TestMetadataIsComplete:
    def test_all_model_modules_are_registered(self) -> None:
        assert len(MODEL_MODULES) == 19

    def test_expected_table_count(self) -> None:
        """A tripwire: a table added or removed without updating the docs fails here."""
        assert len(ALL_TABLES) == 109, f"schema now has {len(ALL_TABLES)} tables"

    def test_every_table_has_a_primary_key(self) -> None:
        """This server enforces ``sql_require_primary_key=ON`` (DEC-042)."""
        for table in ALL_TABLES:
            assert table.primary_key.columns, f"{table.name} has no primary key"

    def test_uuid_primary_keys_are_binary_16(self) -> None:
        """§1.4 / ADR-004: UUIDv7 stored as BINARY(16)."""
        exempt = {"states", "identifier_type_configs", "organization_settings"}
        for table in ALL_TABLES:
            if table.name in exempt:
                continue
            for column in table.primary_key.columns:
                if column.name in {"id", "organization_id"}:
                    assert isinstance(column.type, Uuid7), (
                        f"{table.name}.{column.name} is not a Uuid7 column"
                    )


class TestTenantScoping:
    """§23.1: every organization-scoped table needs ``UNIQUE (organization_id, id)``."""

    def test_org_scoped_tables_are_composite_fk_targets(self) -> None:
        missing: list[str] = []
        for table in ALL_TABLES:
            if table.name in PLATFORM_TABLES or "organization_id" not in table.c:
                continue
            has_pair = any(
                isinstance(constraint, UniqueConstraint)
                and [column.name for column in constraint.columns] == ["organization_id", "id"]
                for constraint in table.constraints
            )
            if not has_pair:
                missing.append(table.name)
        assert not missing, (
            "these organization-scoped tables cannot be composite-FK targets, so §6.3 "
            f"consistency cannot be enforced against them: {missing}"
        )

    def test_employee_master_references_are_composite(self) -> None:
        """§6.3: an employee's master references must be constrained with the organization.

        A single-column FK to ``departments.id`` would happily accept another tenant's
        department, which is exactly what AC-03 forbids.
        """
        employees = Base.metadata.tables["employees"]
        composite_targets = {
            constraint.referred_table.name
            for constraint in employees.foreign_key_constraints
            if len(constraint.columns) > 1
        }
        for required in (
            "departments",
            "designations",
            "grades",
            "business_units",
            "cost_centers",
            "employment_types",
            "branches",
            "work_locations",
            "employee_categories",
            "sub_departments",
            "employees",
        ):
            assert required in composite_targets, (
                f"employees -> {required} is not a composite (organization-scoped) foreign key"
            )

    def test_previous_employment_record_is_the_only_single_column_employee_fk(self) -> None:
        """§6.4: the cross-organization link is deliberately *not* composite."""
        employees = Base.metadata.tables["employees"]
        single_column_self_fks = {
            next(iter(constraint.columns)).name
            for constraint in employees.foreign_key_constraints
            if len(constraint.columns) == 1 and constraint.referred_table.name == "employees"
        }
        assert single_column_self_fks == {"previous_employment_record_id"}


class TestAppendOnlyTables:
    @pytest.mark.parametrize("table_name", sorted(APPEND_ONLY_TABLES))
    def test_has_no_update_columns(self, table_name: str) -> None:
        """No ``updated_at``, ``updated_by`` or ``row_version``: these rows are never updated."""
        table = Base.metadata.tables[table_name]
        forbidden = {"updated_at", "updated_by", "row_version"} & set(table.c.keys())
        assert not forbidden, f"{table_name} has update columns {sorted(forbidden)} (§1.5)"

    @pytest.mark.parametrize("table_name", sorted(APPEND_ONLY_TABLES - {"employee_code_registry"}))
    def test_has_no_archival_columns(self, table_name: str) -> None:
        """Append-only rows are not archived either; retention purges remove them wholesale."""
        table = Base.metadata.tables[table_name]
        forbidden = {"archived_at", "archived_by", "archive_reason"} & set(table.c.keys())
        assert not forbidden, f"{table_name} has archival columns {sorted(forbidden)}"

    def test_audit_log_has_no_foreign_key_to_entity(self) -> None:
        """``entity_id`` must not be a foreign key.

        The audit trail has to outlive hard deletions of never-referenced configuration rows
        and retention purges; a foreign key would either block the purge or cascade the
        evidence away with it.
        """
        audit_logs = Base.metadata.tables["audit_logs"]
        assert not audit_logs.c.entity_id.foreign_keys


class TestMysqlReservedWords:
    """A migration that names a reserved word bare in raw SQL fails at CREATE TABLE.

    Two columns in this schema are reserved words — ``grades.rank`` and
    ``employee_id_generation_rules.separator`` — and both cost a failed, partially applied
    migration before being quoted. This test makes the next one a test failure instead.
    """

    RESERVED: ClassVar[set[str]] = {word.lower() for word in RESERVED_WORDS_MYSQL}

    def test_reserved_column_names_are_quoted_in_check_constraints(self) -> None:
        offenders: list[str] = []
        reserved_columns = {
            column.name.lower()
            for table in ALL_TABLES
            for column in table.columns
            if column.name.lower() in self.RESERVED
        }
        for table in ALL_TABLES:
            for constraint in table.constraints:
                if not isinstance(constraint, CheckConstraint):
                    continue
                sql = str(constraint.sqltext)
                for name in reserved_columns:
                    # Bare means not already wrapped in a backtick or a double quote.
                    if re.search(rf'(?<![`"\w]){re.escape(name)}(?![`"\w])', sql, re.IGNORECASE):
                        offenders.append(f"{table.name}.{constraint.name} -> {name}")
        assert not offenders, (
            "MySQL reserved words referenced unquoted in CHECK SQL; wrap them in backticks: "
            f"{offenders}"
        )

    def test_known_reserved_columns_are_still_the_only_ones(self) -> None:
        """If a new reserved-word column appears, quote it in any raw SQL that names it."""
        reserved_columns = {
            f"{table.name}.{column.name}"
            for table in ALL_TABLES
            for column in table.columns
            if column.name.lower() in self.RESERVED
        }
        assert reserved_columns == {
            "grades.rank",
            "employee_id_generation_rules.separator",
            "background_job_runs.trigger",
        }


class TestGeneratedColumnEmulation:
    """ADR-003: stored generated columns stand in for PostgreSQL partial/exclusion constraints.

    Each entry below is a spec constraint that MySQL cannot express declaratively. If a column
    disappears, the constraint silently stops being enforced at the database level — so the
    presence of each one is asserted explicitly rather than inferred.
    """

    EXPECTED: ClassVar[dict[str, tuple[str, str]]] = {
        # table: (generated column, the spec constraint it enforces)
        "employees": ("employee_code_normalized", "C-02 employee code unique per organization"),
        "employee_identifiers": (
            "current_identifier_key",
            "C-06 one current identifier per type",
        ),
        "employee_bank_accounts": ("primary_from_key", "C-07 one primary bank account"),
        "employee_salary_assignments": (
            "confirmed_from",
            "INV-1 / C-05 no two confirmed assignments starting on the same date",
        ),
        "employee_id_generation_rules": ("conflict_key", "C-11 no conflicting ID rules"),
        "user_organization_access": ("active_grant_key", "§23.3 one active grant per role"),
        "organizations": ("pan_unique_key", "§23.3 PAN unique except for proprietorships"),
        "organization_registrations": (
            "unique_scope_key",
            "§7.4 four different uniqueness scopes in one index",
        ),
        "leave_policy_versions": ("draft_key", "§6.3 one DRAFT version per policy"),
        "leave_application_slots": ("active_key", "§6.8 LEV-26 no overlapping ACTIVE slot"),
        "background_job_runs": ("running_key", "§14.2 one RUNNING run per job"),
        "attendance_policy_versions": ("draft_key", "§5.4 one DRAFT version per policy"),
    }

    @pytest.mark.parametrize(("table_name", "expected"), sorted(EXPECTED.items()))
    def test_generated_column_exists(self, table_name: str, expected: tuple[str, str]) -> None:
        column_name, rule = expected
        table = Base.metadata.tables[table_name]
        assert column_name in table.c, f"{table_name}.{column_name} is missing; it enforces {rule}"
        assert table.c[column_name].computed is not None, (
            f"{table_name}.{column_name} must be a generated column"
        )
        assert table.c[column_name].computed.persisted, (
            f"{table_name}.{column_name} must be STORED, not VIRTUAL, to be indexable"
        )

    @pytest.mark.parametrize(("table_name", "expected"), sorted(EXPECTED.items()))
    def test_generated_column_is_actually_used_by_a_unique_constraint(
        self, table_name: str, expected: tuple[str, str]
    ) -> None:
        """A generated column with no unique constraint on it enforces nothing."""
        column_name, rule = expected
        table = Base.metadata.tables[table_name]
        used = any(
            isinstance(constraint, UniqueConstraint)
            and column_name in {column.name for column in constraint.columns}
            for constraint in table.constraints
        )
        assert used, f"{table_name}.{column_name} is not part of a unique constraint ({rule})"

    def test_master_code_normalization_is_universal(self) -> None:
        """§9.4: master codes are compared case-insensitively, so every master needs the key."""
        from app.models.master import MASTER_MODELS

        for model in MASTER_MODELS.values():
            table = model.__table__  # type: ignore[attr-defined]
            assert "code_normalized" in table.c, f"{table.name} lacks code_normalized"


class TestSensitiveColumnTypes:
    """§11.1, §14.1: encrypted values and blind indexes must be indexable VARBINARY."""

    SENSITIVE: ClassVar[list[tuple[str, str, int]]] = [
        ("employee_identifiers", "value_encrypted", 512),
        ("employee_identifiers", "value_blind_index", 32),
        ("employee_bank_accounts", "account_number_encrypted", 512),
        ("employee_bank_accounts", "account_number_blind_index", 32),
        ("employee_documents", "document_number_encrypted", 512),
    ]

    @pytest.mark.parametrize(("table_name", "column_name", "length"), SENSITIVE)
    def test_is_varbinary_of_expected_length(
        self, table_name: str, column_name: str, length: int
    ) -> None:
        column = Base.metadata.tables[table_name].c[column_name]
        assert column.type.__class__.__name__ == "VARBINARY", (
            f"{table_name}.{column_name} is {column.type}; LargeBinary becomes BLOB on MySQL "
            "and cannot be indexed without a key length"
        )
        assert column.type.length == length

    def test_no_plaintext_identifier_columns_exist(self) -> None:
        """§11.1 stores identifiers as rows of ciphertext, never as plaintext columns.

        A column literally named ``pan`` or ``aadhaar_number`` on ``employees`` would be the
        single most damaging regression possible here, so it is asserted against directly.
        """
        employees = Base.metadata.tables["employees"]
        forbidden = {
            "pan",
            "aadhaar_number",
            "aadhaar",
            "uan",
            "pf_member_id",
            "esic_ip_number",
            "passport_number",
            "voter_id",
            "driving_license_number",
            "bank_account_number",
        }
        present = forbidden & set(employees.c.keys())
        assert not present, f"plaintext identifier columns on employees: {sorted(present)}"

    def test_blind_indexes_are_indexed(self) -> None:
        """§11.5 and §26.2 are equality lookups on these columns; unindexed they are scans."""
        identifiers = Base.metadata.tables["employee_identifiers"]
        indexed_columns = {column.name for index in identifiers.indexes for column in index.columns}
        assert "value_blind_index" in indexed_columns


class TestScopeBoundary:
    """AC-24 / §4.2: out-of-scope HRMS tables must not exist.

    Addendum §2.4 moved leave, attendance and shifts into scope, so those fragments are no
    longer listed.
    """

    FORBIDDEN_TABLE_FRAGMENTS: ClassVar[tuple[str, ...]] = (
        "payroll",
        "payslip",
        "timesheet",
        "expense",
        "reimbursement",
        "asset",
        "appraisal",
        "performance",
        "training",
        "recruit",
        "applicant",
        "interview",
        "tax_declaration",
        "challan",
        "gratuity_calculation",
        "settlement",
        "invoice",
        "journal",
        "loan",
    )

    @pytest.mark.parametrize("fragment", FORBIDDEN_TABLE_FRAGMENTS)
    def test_no_out_of_scope_table(self, fragment: str) -> None:
        offenders = [table.name for table in ALL_TABLES if fragment in table.name]
        assert not offenders, f"§4.2 forbids these modules; found tables: {offenders}"

    def test_no_computed_total_columns(self) -> None:
        """§4.3: no stored net pay, gross total or sum of components."""
        forbidden_names = {
            "net_pay",
            "gross_pay",
            "gross_salary",
            "total_earnings",
            "total_deductions",
            "ctc_total",
            "take_home",
            "annual_total",
            "monthly_total",
            "salary_delta",
            "increment_percentage",
            "cost_to_company",
        }
        offenders = [
            f"{table.name}.{column.name}"
            for table in ALL_TABLES
            for column in table.columns
            if column.name in forbidden_names
        ]
        assert not offenders, f"§4.3 forbids computed compensation values: {offenders}"
