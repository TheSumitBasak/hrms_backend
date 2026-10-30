"""Organization-scoped master data, lookups and custom statuses (§9, §10.2).

Nine master tables share the §9.1 common field set. They are **separate tables, not one
polymorphic table with a `type` discriminator**, because §6.3 requires a distinct composite
foreign key per relationship (`(organization_id, department_id) → departments`) and several
masters carry their own fields (§9.2). A single table would make those foreign keys
impossible and the per-master columns nullable-by-convention.

The shared columns come from :class:`MasterColumns`, so the common shape is declared once
while each table keeps its own constraints and real foreign keys.
"""

from __future__ import annotations

import uuid
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Computed,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    SmallInteger,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, declared_attr, mapped_column

from app.db.base import Base
from app.db.mixins import StandardColumns, archive_reason_constraint, status_constraint
from app.db.types import Uuid7

CODE_PATTERN_SQL = "'^[A-Z0-9][A-Z0-9_./-]{0,29}$'"
"""§21.1 V-CODE."""

LOOKUP_VALUE_PATTERN_SQL = "'^[A-Z0-9_]{1,50}$'"
"""§9.3 ``value_code``."""


class MasterColumns(StandardColumns):
    """The §9.1 common master fields, shared by all nine master tables."""

    @declared_attr
    def organization_id(self) -> Mapped[uuid.UUID]:
        return mapped_column(
            Uuid7, ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
        )

    @declared_attr
    def code(self) -> Mapped[str]:
        return mapped_column(String(30), nullable=False)

    @declared_attr
    def code_normalized(self) -> Mapped[str]:
        """Case-insensitive uniqueness key (§9.4, §23.1).

        A stored generated column because MySQL has no functional index; the unique constraint
        on it reproduces PostgreSQL's ``unique (organization_id, upper(code))``.
        """
        return mapped_column(String(30), Computed("UPPER(code)", persisted=True), nullable=False)

    @declared_attr
    def name(self) -> Mapped[str]:
        return mapped_column(String(150), nullable=False)

    @declared_attr
    def description(self) -> Mapped[str | None]:
        return mapped_column(String(500), nullable=True)

    @declared_attr
    def display_order(self) -> Mapped[int | None]:
        return mapped_column(Integer, nullable=True)

    @declared_attr
    def status(self) -> Mapped[str]:
        return mapped_column(String(10), nullable=False, default="ACTIVE", server_default="ACTIVE")


def master_table_args(table_name: str, *extra: Any) -> tuple[Any, ...]:
    """Constraints every master table needs, plus any table-specific ones.

    Codes are unique per organization and master type, including inactive and archived rows:
    §23.4 says master codes are never reused.
    """
    return (
        UniqueConstraint("organization_id", "code_normalized", name=f"uq_{table_name}_org_code"),
        UniqueConstraint("organization_id", "id", name=f"uq_{table_name}_org_id"),
        CheckConstraint(f"code REGEXP {CODE_PATTERN_SQL}", name=f"ck_{table_name}_code_pattern"),
        CheckConstraint("CHAR_LENGTH(name) BETWEEN 2 AND 150", name=f"ck_{table_name}_name_length"),
        CheckConstraint(
            "display_order IS NULL OR display_order >= 0", name=f"ck_{table_name}_display_order"
        ),
        status_constraint(table_name),
        archive_reason_constraint(table_name),
        Index(f"ix_{table_name}_org_status_order", "organization_id", "status", "display_order"),
        *extra,
    )


class Department(Base, MasterColumns):
    """§9.1 + §9.2. ``code`` feeds the ``{DEPT}`` token of an employee code (§19.1)."""

    __tablename__ = "departments"
    __table_args__ = master_table_args(
        "departments",
        # §6.3: the head must be an employee of the same organization.
        ForeignKeyConstraint(
            ["organization_id", "head_employee_id"],
            ["employees.organization_id", "employees.id"],
            name="fk_departments_head_employee",
            ondelete="RESTRICT",
            use_alter=True,
        ),
    )

    head_employee_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    """§5.4: a Department Manager sees employees of a department they head."""


class SubDepartment(Base, MasterColumns):
    """§9.2. ``code`` is unique within the organization, not merely within the department."""

    __tablename__ = "sub_departments"
    __table_args__ = master_table_args(
        "sub_departments",
        ForeignKeyConstraint(
            ["organization_id", "department_id"],
            ["departments.organization_id", "departments.id"],
            name="fk_sub_departments_department",
            ondelete="RESTRICT",
        ),
        # §6.3: lets `employees` constrain (department_id, sub_department_id) as a pair, so a
        # sub-department from a different department cannot be assigned.
        UniqueConstraint("department_id", "id", name="uq_sub_departments_department_id"),
    )

    department_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)


class Designation(Base, MasterColumns):
    """§9.2. ``default_grade_id`` pre-fills the grade on the employee form."""

    __tablename__ = "designations"
    __table_args__ = master_table_args(
        "designations",
        ForeignKeyConstraint(
            ["organization_id", "default_grade_id"],
            ["grades.organization_id", "grades.id"],
            name="fk_designations_default_grade",
            ondelete="RESTRICT",
            use_alter=True,
        ),
    )

    default_grade_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)


class Grade(Base, MasterColumns):
    """§9.2. ``rank`` is unique within the organization; lower is more junior (A-10)."""

    __tablename__ = "grades"
    __table_args__ = master_table_args(
        "grades",
        UniqueConstraint("organization_id", "rank", name="uq_grades_org_rank"),
        # `rank` became a reserved word in MySQL 8 (window functions), so it must be
        # quoted inside raw constraint SQL. Backticks work regardless of whether the
        # server runs with ANSI_QUOTES, which this one does (DEC-041).
        CheckConstraint("`rank` BETWEEN 1 AND 999", name="ck_grades_rank_range"),
    )

    rank: Mapped[int] = mapped_column(SmallInteger, nullable=False)


class BusinessUnit(Base, MasterColumns):
    """§9.2."""

    __tablename__ = "business_units"
    __table_args__ = master_table_args(
        "business_units",
        ForeignKeyConstraint(
            ["organization_id", "head_employee_id"],
            ["employees.organization_id", "employees.id"],
            name="fk_business_units_head_employee",
            ondelete="RESTRICT",
            use_alter=True,
        ),
    )

    head_employee_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)


class CostCenter(Base, MasterColumns):
    """§9.2. A label for reporting only — §4.2 excludes accounting entirely."""

    __tablename__ = "cost_centers"
    __table_args__ = master_table_args(
        "cost_centers",
        ForeignKeyConstraint(
            ["organization_id", "business_unit_id"],
            ["business_units.organization_id", "business_units.id"],
            name="fk_cost_centers_business_unit",
            ondelete="RESTRICT",
        ),
    )

    business_unit_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)


class EmploymentTypeBase(StrEnum):
    """§9.2 ``base_type``. Seeded one row per member when an organization is created."""

    PERMANENT = "PERMANENT"
    PROBATION = "PROBATION"
    FIXED_TERM = "FIXED_TERM"
    CONTRACT = "CONTRACT"
    TRAINEE = "TRAINEE"
    INTERN = "INTERN"
    CONSULTANT = "CONSULTANT"
    OTHER = "OTHER"


class EmploymentType(Base, MasterColumns):
    """§9.2. ``requires_end_date`` drives EMP-04 at activation."""

    __tablename__ = "employment_types"
    __table_args__ = master_table_args(
        "employment_types",
        CheckConstraint(
            "base_type IN (" + ",".join(f"'{member.value}'" for member in EmploymentTypeBase) + ")",
            name="ck_employment_types_base_type",
        ),
    )

    base_type: Mapped[str] = mapped_column(String(20), nullable=False)
    """Immutable once referenced (§9.2)."""

    requires_end_date: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    """Defaults to true for ``FIXED_TERM``, false otherwise — set by the seeder, since the
    default depends on ``base_type``."""


class WorkLocation(Base, MasterColumns):
    """§9.2, with the §7.1 address group required when neither remote nor branch-based."""

    __tablename__ = "work_locations"
    __table_args__ = master_table_args(
        "work_locations",
        ForeignKeyConstraint(
            ["organization_id", "branch_id"],
            ["branches.organization_id", "branches.id"],
            name="fk_work_locations_branch",
            ondelete="RESTRICT",
        ),
        # §9.2: the address is required when the location is neither remote nor attached to a
        # branch, because otherwise nothing would record where it is.
        CheckConstraint(
            "is_remote = 1 OR branch_id IS NOT NULL OR ("
            "address_line_1 IS NOT NULL AND address_city IS NOT NULL "
            "AND address_state IS NOT NULL AND address_postal_code IS NOT NULL "
            "AND address_country IS NOT NULL)",
            name="ck_work_locations_address_required",
        ),
    )

    branch_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    is_remote: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )

    address_line_1: Mapped[str | None] = mapped_column(String(200), nullable=True)
    address_line_2: Mapped[str | None] = mapped_column(String(200), nullable=True)
    address_city: Mapped[str | None] = mapped_column(String(100), nullable=True)
    address_district: Mapped[str | None] = mapped_column(String(100), nullable=True)
    address_state: Mapped[str | None] = mapped_column(String(100), nullable=True)
    address_state_code: Mapped[str | None] = mapped_column(String(2), nullable=True)
    address_country: Mapped[str | None] = mapped_column(String(2), nullable=True)
    address_postal_code: Mapped[str | None] = mapped_column(String(10), nullable=True)


class EmployeeCategory(Base, MasterColumns):
    """§9.2: no master-specific fields. For example Staff, Workman, Management."""

    __tablename__ = "employee_categories"
    __table_args__ = master_table_args("employee_categories")


class LookupList(StrEnum):
    """§9.3 ``list_code``. The nineteen configurable option lists."""

    GENDER = "GENDER"
    MARITAL_STATUS = "MARITAL_STATUS"
    BLOOD_GROUP = "BLOOD_GROUP"
    RELATIONSHIP = "RELATIONSHIP"
    QUALIFICATION_LEVEL = "QUALIFICATION_LEVEL"
    STUDY_MODE = "STUDY_MODE"
    SOURCE_OF_HIRE = "SOURCE_OF_HIRE"
    DISABILITY_TYPE = "DISABILITY_TYPE"
    BANK_ACCOUNT_TYPE = "BANK_ACCOUNT_TYPE"
    INDUSTRY = "INDUSTRY"
    BGV_STATUS = "BGV_STATUS"
    STATUS_REASON = "STATUS_REASON"
    JOB_CHANGE_TYPE = "JOB_CHANGE_TYPE"
    SALARY_REVISION_REASON = "SALARY_REVISION_REASON"
    DOCUMENT_TYPE = "DOCUMENT_TYPE"
    RELIGION = "RELIGION"
    SOCIAL_CATEGORY = "SOCIAL_CATEGORY"
    COMPETENCY_CATEGORY = "COMPETENCY_CATEGORY"
    """JA-SPEC-001 §9.5. Adding a member rebuilds ``ck_lookup_values_list_code``, which
    enumerates the list codes; the position/job migration does exactly that."""
    ATTENDANCE_SOURCE = "ATTENDANCE_SOURCE"
    """Addendum §9.4: registered integration source codes. Nothing is seeded."""


class LookupValue(Base, StandardColumns):
    """One configurable option value (§9.3).

    Seeded per organization on creation. An inactive value cannot be selected for new or
    changed data, but existing references stay valid and keep displaying their label — which
    is why referencing rows store ``value_code`` rather than a foreign key to this table's
    ``id``: a label edit must not rewrite history, and a lookup row must not be deletable out
    from under a historical record.
    """

    __tablename__ = "lookup_values"
    __table_args__ = (
        UniqueConstraint(
            "organization_id", "list_code", "value_code", name="uq_lookup_values_org_list_value"
        ),
        UniqueConstraint("organization_id", "id", name="uq_lookup_values_org_id"),
        CheckConstraint(
            f"value_code REGEXP {LOOKUP_VALUE_PATTERN_SQL}", name="ck_lookup_values_value_code"
        ),
        CheckConstraint("CHAR_LENGTH(label) BETWEEN 1 AND 100", name="ck_lookup_values_label"),
        CheckConstraint("display_order >= 0", name="ck_lookup_values_display_order"),
        CheckConstraint(
            "list_code IN (" + ",".join(f"'{member.value}'" for member in LookupList) + ")",
            name="ck_lookup_values_list_code",
        ),
        status_constraint("lookup_values"),
        archive_reason_constraint("lookup_values"),
        Index(
            "ix_lookup_values_org_list_status",
            "organization_id",
            "list_code",
            "status",
            "display_order",
        ),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid7, ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    list_code: Mapped[str] = mapped_column(String(40), nullable=False)
    value_code: Mapped[str] = mapped_column(String(50), nullable=False)
    """Immutable (§9.3): referencing rows store this value."""

    label: Mapped[str] = mapped_column(String(100), nullable=False)
    display_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="ACTIVE")
    is_system_default: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    """Seeded values. They can be deactivated but never deleted (§9.3), and ``UNDISCLOSED``
    in the GENDER list cannot even be deactivated."""


class CustomStatusBehaviour(StrEnum):
    """§10.2 ``base_behaviour``: a custom status inherits the rules of one of these."""

    ACTIVE_LIKE = "ACTIVE_LIKE"
    SEPARATED = "SEPARATED"


class EmployeeCustomStatus(Base, StandardColumns):
    """An organization-defined employee status (§10.2), for example ``TRANSFERRED``.

    Validation and transition rules are inherited from ``base_behaviour``, so a custom status
    never introduces new business rules — only a new label for existing ones. Separated-like
    custom statuses are terminal, exactly as ``RESIGNED`` is.
    """

    __tablename__ = "employee_custom_statuses"
    __table_args__ = (
        UniqueConstraint("organization_id", "code", name="uq_employee_custom_statuses_org_code"),
        UniqueConstraint("organization_id", "id", name="uq_employee_custom_statuses_org_id"),
        CheckConstraint("code REGEXP '^[A-Z_]{2,30}$'", name="ck_employee_custom_statuses_code"),
        CheckConstraint(
            "base_behaviour IN ('ACTIVE_LIKE','SEPARATED')",
            name="ck_employee_custom_statuses_base_behaviour",
        ),
        status_constraint("employee_custom_statuses"),
        archive_reason_constraint("employee_custom_statuses"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid7, ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    code: Mapped[str] = mapped_column(String(30), nullable=False)
    label: Mapped[str] = mapped_column(String(100), nullable=False)
    base_behaviour: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="ACTIVE")


MASTER_MODELS: dict[str, type[Base]] = {
    "departments": Department,
    "sub-departments": SubDepartment,
    "designations": Designation,
    "grades": Grade,
    "business-units": BusinessUnit,
    "cost-centers": CostCenter,
    "employment-types": EmploymentType,
    "work-locations": WorkLocation,
    "employee-categories": EmployeeCategory,
}
"""§24.4.2 path segment → model. One registry drives the generic master endpoints, so adding
a master type does not mean adding nine routes (DEC-023)."""
