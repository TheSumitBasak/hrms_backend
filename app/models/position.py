"""Job architecture: job families, jobs, competencies, positions and headcount (JA-SPEC-001).

The module models six ideas the base EMS deliberately did not have, and keeps them apart:

* a **job** (``jobs``) is reusable work — no department, no incumbent, no headcount;
* a **position** (``positions``) is one authorized seat, with a department, a grade and a
  capacity;
* **occupancy** (``employee_position_assignments``) is the effective-dated link between an
  employee and a position, and is the *only* authoritative occupancy record (JA §2.2);
* a **headcount plan** (``headcount_plans``) is a scoped cap on establishment, not an
  allocation;
* a **headcount change request** (``headcount_change_requests``) is the single maker-checker
  path by which a position's or plan's authorized capacity may change (JA §8.7);
* a **competency** is a *requirement* on a job or position, never an employee assessment
  (JA §9.4 — an employee competency table is explicitly out of scope).

Two reuse rules are structural rather than advisory. There is no grade or job-level table
here: ``grades`` is the only grade catalogue and ``grades.rank`` the only ordering authority
(JA §6.1). And ``employees`` gains no current-position column, because that would be a second
source of truth for occupancy (JA §1.6).

Every organization-scoped reference is a **composite** foreign key on
``(organization_id, x_id)``, so the database itself refuses a position whose job, department
or grade belongs to another tenant — layer 4 of the §6.1 isolation model. Case-insensitive
code uniqueness uses stored generated ``*_normalized`` columns, as elsewhere in this schema,
because MySQL has no functional index.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    CheckConstraint,
    Computed,
    Date,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.mixins import (
    AppendOnlyColumns,
    StandardColumns,
    archive_reason_constraint,
    status_constraint,
    tenant_index,
)
from app.db.types import Uuid7

CODE_PATTERN_SQL = "'^[A-Z0-9][A-Z0-9_./-]{0,29}$'"
"""§21.1 V-CODE, the same pattern the nine masters use."""

MIN_PROFICIENCY = 1
MAX_PROFICIENCY = 5
"""JA §9.2: a fixed 1-5 scale. There is no proficiency-level table and no custom scale."""

POSITION_REPORTING_MAX_DEPTH = 50
"""JA §7.5: the position chain is walked to fifty levels, matching EMP-06 for employees."""

JOB_FAMILY_MAX_DEPTH = 2
"""JA §5.1: ``FAMILY -> SUBFAMILY`` and no deeper."""

HEADCOUNT_REASON_MIN_LENGTH = 20
"""JA §8.7: a change request must say why in at least twenty characters."""


class JobNodeLevel(StrEnum):
    """JA §5.2 ``job_families.node_level``."""

    FAMILY = "FAMILY"
    SUBFAMILY = "SUBFAMILY"


class CatalogueStatus(StrEnum):
    """The ``ACTIVE``/``INACTIVE`` status every catalogue row in this module carries."""

    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"


class CompetencyRequirementType(StrEnum):
    """JA §9.3. ``REQUIRED`` wins over ``PREFERRED`` when job and position mappings merge."""

    REQUIRED = "REQUIRED"
    PREFERRED = "PREFERRED"


class PositionStatus(StrEnum):
    """JA §7.4 operational status. Archival is ``archived_at``, not a status value."""

    DRAFT = "DRAFT"
    OPEN = "OPEN"
    FROZEN = "FROZEN"
    CLOSED = "CLOSED"


class PositionWorkMode(StrEnum):
    """JA §7.2 ``positions.work_mode``, the same vocabulary as ``employees.work_mode``."""

    ON_SITE = "ON_SITE"
    HYBRID = "HYBRID"
    REMOTE = "REMOTE"
    OTHER = "OTHER"


class AssignmentType(StrEnum):
    """JA §10.8. One active ``PRIMARY`` per employee; the others may coexist."""

    PRIMARY = "PRIMARY"
    ACTING = "ACTING"
    SECONDARY = "SECONDARY"


class AssignmentStatus(StrEnum):
    """JA §15.3. Only ``ACTIVE`` rows count toward occupancy."""

    SCHEDULED = "SCHEDULED"
    ACTIVE = "ACTIVE"
    ENDED = "ENDED"
    CANCELLED = "CANCELLED"


class AssignmentSource(StrEnum):
    """JA §10.8 ``source``: which path created the row."""

    MANUAL = "MANUAL"
    ONBOARDING = "ONBOARDING"
    TRANSFER = "TRANSFER"
    SYSTEM = "SYSTEM"


class HeadcountScopeType(StrEnum):
    """JA §8.6. A plan has exactly one scope."""

    ORGANIZATION = "ORGANIZATION"
    DEPARTMENT = "DEPARTMENT"
    BRANCH = "BRANCH"
    WORK_LOCATION = "WORK_LOCATION"
    JOB = "JOB"
    GRADE = "GRADE"


class HeadcountPlanStatus(StrEnum):
    """JA §8.6. Only ``ACTIVE`` plans are enforced; ``ARCHIVED`` is the plan's archive state."""

    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    ARCHIVED = "ARCHIVED"


class HeadcountChangeType(StrEnum):
    """JA §8.7."""

    INCREASE = "INCREASE"
    DECREASE = "DECREASE"
    SET = "SET"


class HeadcountRequestStatus(StrEnum):
    """JA §15.5."""

    DRAFT = "DRAFT"
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


class ReservationSourceType(StrEnum):
    """JA §8.8. A reservation is not a requisition — no recruitment module is implied."""

    OFFER = "OFFER"
    ONBOARDING = "ONBOARDING"
    TRANSFER = "TRANSFER"
    MANUAL = "MANUAL"


class ReservationStatus(StrEnum):
    """JA §15.6."""

    ACTIVE = "ACTIVE"
    RELEASED = "RELEASED"
    EXPIRED = "EXPIRED"


ACTIVE_ASSIGNMENT_STATUSES: frozenset[str] = frozenset({AssignmentStatus.ACTIVE.value})
"""JA §8.3: only ``ACTIVE`` rows are counted. ``SCHEDULED`` does not consume capacity until
its effective date, and ``ENDED``/``CANCELLED`` never do."""

OCCUPYING_ASSIGNMENT_STATUSES: frozenset[str] = frozenset(
    {AssignmentStatus.ACTIVE.value, AssignmentStatus.SCHEDULED.value}
)
"""JA §7.8: a position cannot be closed or archived while either of these exists, even
though ``SCHEDULED`` contributes nothing to today's occupancy."""

ESTABLISHMENT_POSITION_STATUSES: frozenset[str] = frozenset(
    {PositionStatus.OPEN.value, PositionStatus.FROZEN.value}
)
"""JA §8.4: a plan authorizes *establishment*, so the authorized capacity of every matching
``OPEN`` or ``FROZEN`` position counts against it. ``DRAFT``, ``CLOSED`` and archived
positions do not."""


def _enum_check(column: str, enum: type[StrEnum], name: str) -> CheckConstraint:
    values = ",".join(f"'{member.value}'" for member in enum)
    return CheckConstraint(f"{column} IN ({values})", name=name)


def _code_checks(
    table_name: str, column: str, *, name_column: str, name_max: int
) -> tuple[CheckConstraint, CheckConstraint]:
    return (
        CheckConstraint(
            f"{column} REGEXP {CODE_PATTERN_SQL}", name=f"ck_{table_name}_code_pattern"
        ),
        CheckConstraint(
            f"CHAR_LENGTH({name_column}) BETWEEN 2 AND {name_max}",
            name=f"ck_{table_name}_name_length",
        ),
    )


def _organization_column() -> Mapped[uuid.UUID]:
    return mapped_column(
        Uuid7, ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False, index=True
    )


# =====================================================================================
# Catalogue: families, jobs, competencies
# =====================================================================================


class JobFamily(Base, StandardColumns):
    """A job family or subfamily (JA §5).

    One self-referencing table rather than two near-identical ones: the parent-child link is
    then a real, declarative foreign key, and "filter by a family and include its
    subfamilies" is one join instead of a union. Depth is capped at two by the service; the
    database contributes the self-parent CHECK and the level/parent consistency CHECK, which
    together make a one-row cycle impossible by any write path.
    """

    __tablename__ = "job_families"
    __table_args__ = (
        UniqueConstraint(
            "organization_id", "family_code_normalized", name="uq_job_families_org_code"
        ),
        UniqueConstraint("organization_id", "id", name="uq_job_families_org_id"),
        ForeignKeyConstraint(
            ["organization_id", "parent_job_family_id"],
            ["job_families.organization_id", "job_families.id"],
            name="fk_job_families_parent",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "parent_job_family_id IS NULL OR parent_job_family_id <> id",
            name="ck_job_families_no_self_parent",
        ),
        # JA §5.1: a FAMILY has no parent and a SUBFAMILY must have one. Without this the
        # "maximum depth two" rule would rest on the service alone.
        CheckConstraint(
            "(node_level = 'FAMILY' AND parent_job_family_id IS NULL) OR "
            "(node_level = 'SUBFAMILY' AND parent_job_family_id IS NOT NULL)",
            name="ck_job_families_level_parent",
        ),
        *_code_checks("job_families", "family_code", name_column="name", name_max=150),
        CheckConstraint(
            "display_order IS NULL OR display_order >= 0", name="ck_job_families_display_order"
        ),
        _enum_check("node_level", JobNodeLevel, "ck_job_families_node_level"),
        status_constraint("job_families"),
        archive_reason_constraint("job_families"),
        tenant_index(
            "job_families",
            "parent_job_family_id",
            "status",
            name="ix_job_families_org_parent_status",
        ),
        tenant_index(
            "job_families", "status", "display_order", "name", name="ix_job_families_org_listing"
        ),
    )

    organization_id: Mapped[uuid.UUID] = _organization_column()
    family_code: Mapped[str] = mapped_column(String(30), nullable=False)
    family_code_normalized: Mapped[str] = mapped_column(
        String(30), Computed("UPPER(family_code)", persisted=True), nullable=False
    )
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    description: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    node_level: Mapped[str] = mapped_column(String(10), nullable=False)
    parent_job_family_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    display_order: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(
        String(10), nullable=False, default=CatalogueStatus.ACTIVE.value, server_default="ACTIVE"
    )


class Job(Base, StandardColumns):
    """A reusable job definition (JA §4).

    A job has no department, branch, incumbent or FTE — those belong to a position, because
    the same job legitimately exists in several departments (JA §4.1). The three grade
    columns are *classification* only: the grade range is validated by ``grades.rank`` in the
    service, since a rank comparison spans rows and no CHECK can express it.
    """

    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("organization_id", "job_code_normalized", name="uq_jobs_org_code"),
        UniqueConstraint("organization_id", "id", name="uq_jobs_org_id"),
        ForeignKeyConstraint(
            ["organization_id", "job_family_id"],
            ["job_families.organization_id", "job_families.id"],
            name="fk_jobs_job_family",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "default_grade_id"],
            ["grades.organization_id", "grades.id"],
            name="fk_jobs_default_grade",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "min_grade_id"],
            ["grades.organization_id", "grades.id"],
            name="fk_jobs_min_grade",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "max_grade_id"],
            ["grades.organization_id", "grades.id"],
            name="fk_jobs_max_grade",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "default_designation_id"],
            ["designations.organization_id", "designations.id"],
            name="fk_jobs_default_designation",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "default_employment_type_id"],
            ["employment_types.organization_id", "employment_types.id"],
            name="fk_jobs_default_employment_type",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from",
            name="ck_jobs_effective_range",
        ),
        CheckConstraint(
            "min_experience_years IS NULL OR min_experience_years BETWEEN 0 AND 50",
            name="ck_jobs_min_experience",
        ),
        *_code_checks("jobs", "job_code", name_column="title", name_max=200),
        status_constraint("jobs"),
        archive_reason_constraint("jobs"),
        tenant_index("jobs", "job_family_id", "status", "title", name="ix_jobs_org_family_status"),
        tenant_index("jobs", "status", "title", name="ix_jobs_org_status_title"),
        tenant_index("jobs", "title", name="ix_jobs_org_title"),
        tenant_index("jobs", "status", "effective_from", name="ix_jobs_org_status_effective"),
        tenant_index("jobs", "default_grade_id", name="ix_jobs_org_default_grade"),
    )

    organization_id: Mapped[uuid.UUID] = _organization_column()
    job_code: Mapped[str] = mapped_column(String(30), nullable=False)
    job_code_normalized: Mapped[str] = mapped_column(
        String(30), Computed("UPPER(job_code)", persisted=True), nullable=False
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    responsibilities: Mapped[str | None] = mapped_column(Text, nullable=True)
    job_family_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    default_grade_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    min_grade_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    max_grade_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    default_designation_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    default_employment_type_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    min_experience_years: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    required_qualifications: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(10), nullable=False, default=CatalogueStatus.ACTIVE.value, server_default="ACTIVE"
    )
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)


class Competency(Base, StandardColumns):
    """A skill, behaviour or capability a job or position may require (JA §9.1).

    Catalogue data. ``category_code`` stores a ``COMPETENCY_CATEGORY`` lookup *value* rather
    than a foreign key to ``lookup_values.id``, exactly as the rest of the schema does, so
    relabelling a category cannot rewrite history.
    """

    __tablename__ = "competencies"
    __table_args__ = (
        UniqueConstraint(
            "organization_id", "competency_code_normalized", name="uq_competencies_org_code"
        ),
        UniqueConstraint("organization_id", "id", name="uq_competencies_org_id"),
        *_code_checks("competencies", "competency_code", name_column="name", name_max=150),
        CheckConstraint(
            "category_code REGEXP '^[A-Z0-9_]{1,50}$'", name="ck_competencies_category_code"
        ),
        status_constraint("competencies"),
        archive_reason_constraint("competencies"),
        tenant_index(
            "competencies", "category_code", "status", "name", name="ix_competencies_org_category"
        ),
        tenant_index("competencies", "status", "name", name="ix_competencies_org_status_name"),
    )

    organization_id: Mapped[uuid.UUID] = _organization_column()
    competency_code: Mapped[str] = mapped_column(String(30), nullable=False)
    competency_code_normalized: Mapped[str] = mapped_column(
        String(30), Computed("UPPER(competency_code)", persisted=True), nullable=False
    )
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    description: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    category_code: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(
        String(10), nullable=False, default=CatalogueStatus.ACTIVE.value, server_default="ACTIVE"
    )


class JobCompetency(Base, StandardColumns):
    """An effective-dated competency requirement on a job (JA §9.3).

    A mapping change end-dates the previous row and inserts a new one, so the history of what
    a job required survives. ``UNIQUE (organization_id, job_id, competency_id,
    effective_from)`` blocks the same-start duplicate; the service blocks any *overlap* under
    the documented lock order, because MySQL has no exclusion constraint.
    """

    __tablename__ = "job_competencies"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "job_id",
            "competency_id",
            "effective_from",
            name="uq_job_competencies_mapping_start",
        ),
        UniqueConstraint("organization_id", "id", name="uq_job_competencies_org_id"),
        ForeignKeyConstraint(
            ["organization_id", "job_id"],
            ["jobs.organization_id", "jobs.id"],
            name="fk_job_competencies_job",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "competency_id"],
            ["competencies.organization_id", "competencies.id"],
            name="fk_job_competencies_competency",
            ondelete="RESTRICT",
        ),
        _enum_check(
            "requirement_type", CompetencyRequirementType, "ck_job_competencies_requirement_type"
        ),
        CheckConstraint(
            f"min_proficiency BETWEEN {MIN_PROFICIENCY} AND {MAX_PROFICIENCY}",
            name="ck_job_competencies_proficiency",
        ),
        CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from",
            name="ck_job_competencies_effective_range",
        ),
        CheckConstraint(
            "display_order IS NULL OR display_order >= 0",
            name="ck_job_competencies_display_order",
        ),
        archive_reason_constraint("job_competencies"),
        tenant_index(
            "job_competencies", "job_id", "effective_from", name="ix_job_competencies_org_job"
        ),
        tenant_index("job_competencies", "competency_id", name="ix_job_competencies_org_comp"),
    )

    organization_id: Mapped[uuid.UUID] = _organization_column()
    job_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    competency_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    requirement_type: Mapped[str] = mapped_column(
        String(10),
        nullable=False,
        default=CompetencyRequirementType.REQUIRED.value,
        server_default="REQUIRED",
    )
    min_proficiency: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=1, server_default=text("1")
    )
    display_order: Mapped[int | None] = mapped_column(Integer, nullable=True)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)


# =====================================================================================
# Positions
# =====================================================================================


class Position(Base, StandardColumns):
    """One authorized seat in the organization (JA §7).

    ``authorized_fte`` and ``max_incumbents`` are the two capacity dimensions, enforced
    independently (JA §8.2): 2.00 FTE across at most 3 incumbents means four half-time people
    fit the FTE cap but not the incumbent cap. Neither is editable through ``PATCH`` once the
    position exists — JA §8.7 routes every later change through an approved headcount request,
    so a budget cannot be raised silently.

    There is no cached ``occupied_fte`` column. Occupancy is always derived from
    ``employee_position_assignments`` (JA §7.6), which is what keeps the capacity check and
    the headcount report from ever disagreeing.
    """

    __tablename__ = "positions"
    __table_args__ = (
        UniqueConstraint(
            "organization_id", "position_code_normalized", name="uq_positions_org_code"
        ),
        UniqueConstraint("organization_id", "id", name="uq_positions_org_id"),
        ForeignKeyConstraint(
            ["organization_id", "job_id"],
            ["jobs.organization_id", "jobs.id"],
            name="fk_positions_job",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "department_id"],
            ["departments.organization_id", "departments.id"],
            name="fk_positions_department",
            ondelete="RESTRICT",
        ),
        # §6.3: the pair is constrained, so a sub-department from a *different* department
        # cannot be attached to this position.
        ForeignKeyConstraint(
            ["department_id", "sub_department_id"],
            ["sub_departments.department_id", "sub_departments.id"],
            name="fk_positions_sub_department",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "branch_id"],
            ["branches.organization_id", "branches.id"],
            name="fk_positions_branch",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "work_location_id"],
            ["work_locations.organization_id", "work_locations.id"],
            name="fk_positions_work_location",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "business_unit_id"],
            ["business_units.organization_id", "business_units.id"],
            name="fk_positions_business_unit",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "cost_center_id"],
            ["cost_centers.organization_id", "cost_centers.id"],
            name="fk_positions_cost_center",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "grade_id"],
            ["grades.organization_id", "grades.id"],
            name="fk_positions_grade",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "reports_to_position_id"],
            ["positions.organization_id", "positions.id"],
            name="fk_positions_reports_to",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "reports_to_position_id IS NULL OR reports_to_position_id <> id",
            name="ck_positions_no_self_report",
        ),
        CheckConstraint(
            "authorized_fte > 0 AND authorized_fte <= 999.99", name="ck_positions_authorized_fte"
        ),
        CheckConstraint("max_incumbents BETWEEN 1 AND 999", name="ck_positions_max_incumbents"),
        CheckConstraint(
            "planned_start_date IS NULL OR planned_end_date IS NULL OR "
            "planned_end_date > planned_start_date",
            name="ck_positions_planned_dates",
        ),
        # JA §7.2: the reason and timestamp columns a status implies are not optional.
        CheckConstraint(
            "position_status <> 'FROZEN' OR freeze_reason IS NOT NULL",
            name="ck_positions_freeze_reason",
        ),
        CheckConstraint(
            "position_status <> 'CLOSED' OR close_reason IS NOT NULL",
            name="ck_positions_close_reason",
        ),
        CheckConstraint(
            "position_status <> 'OPEN' OR opened_at IS NOT NULL", name="ck_positions_opened_at"
        ),
        CheckConstraint(
            "position_status <> 'CLOSED' OR closed_at IS NOT NULL", name="ck_positions_closed_at"
        ),
        _enum_check("position_status", PositionStatus, "ck_positions_status"),
        _enum_check("work_mode", PositionWorkMode, "ck_positions_work_mode"),
        *_code_checks("positions", "position_code", name_column="position_title", name_max=200),
        archive_reason_constraint("positions"),
        tenant_index(
            "positions", "position_status", "department_id", name="ix_positions_org_status_dept"
        ),
        tenant_index(
            "positions", "position_status", "position_title", name="ix_positions_org_status_title"
        ),
        tenant_index("positions", "position_title", name="ix_positions_org_title"),
        tenant_index("positions", "job_id", "position_status", name="ix_positions_org_job_status"),
        tenant_index("positions", "reports_to_position_id", name="ix_positions_org_reports_to"),
        tenant_index(
            "positions", "department_id", "sub_department_id", name="ix_positions_org_dept_sub"
        ),
        tenant_index(
            "positions", "branch_id", "position_status", name="ix_positions_org_branch_status"
        ),
        tenant_index("positions", "grade_id", "position_status", name="ix_positions_org_grade"),
        tenant_index(
            "positions", "work_location_id", "position_status", name="ix_positions_org_location"
        ),
        tenant_index("positions", "position_code_normalized", name="ix_positions_org_code_norm"),
    )

    organization_id: Mapped[uuid.UUID] = _organization_column()
    position_code: Mapped[str] = mapped_column(String(30), nullable=False)
    position_code_normalized: Mapped[str] = mapped_column(
        String(30), Computed("UPPER(position_code)", persisted=True), nullable=False
    )
    position_title: Mapped[str] = mapped_column(String(200), nullable=False)
    """JA §7.2: defaults from the job title at creation, then independent — so renaming a job
    does not retroactively rename every position or its history (JA-A-11)."""

    job_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    department_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    """Required in v1 (JA-Q-02): employee activation already requires a department and the
    department scope is what headcount plans are usually written against."""

    sub_department_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    branch_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    work_location_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    work_mode: Mapped[str] = mapped_column(
        String(20), nullable=False, default=PositionWorkMode.ON_SITE.value, server_default="ON_SITE"
    )
    business_unit_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    cost_center_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    grade_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    reports_to_position_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    """The *position* hierarchy. Independent of ``employees.reporting_manager_employee_id``,
    which remains the people hierarchy (JA §7.5)."""

    authorized_fte: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), nullable=False, default=Decimal("1.00"), server_default=text("1.00")
    )
    max_incumbents: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=1, server_default=text("1")
    )
    position_status: Mapped[str] = mapped_column(
        String(10), nullable=False, default=PositionStatus.DRAFT.value, server_default="DRAFT"
    )
    planned_start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    planned_end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    opened_at: Mapped[datetime | None] = mapped_column(nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    freeze_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    close_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)


class PositionCompetency(Base, StandardColumns):
    """A position-specific competency requirement (JA §9.3).

    Mirrors :class:`JobCompetency`, keyed by position. The *addition* rule is a service
    invariant: a position may add a requirement or raise a minimum, never remove or lower
    what the job requires (JA §9.4, ``COMPETENCY_REQUIREMENT_CONFLICT``).
    """

    __tablename__ = "position_competencies"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "position_id",
            "competency_id",
            "effective_from",
            name="uq_position_competencies_start",
        ),
        UniqueConstraint("organization_id", "id", name="uq_position_competencies_org_id"),
        ForeignKeyConstraint(
            ["organization_id", "position_id"],
            ["positions.organization_id", "positions.id"],
            name="fk_position_competencies_position",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "competency_id"],
            ["competencies.organization_id", "competencies.id"],
            name="fk_position_competencies_competency",
            ondelete="RESTRICT",
        ),
        _enum_check(
            "requirement_type",
            CompetencyRequirementType,
            "ck_position_competencies_requirement_type",
        ),
        CheckConstraint(
            f"min_proficiency BETWEEN {MIN_PROFICIENCY} AND {MAX_PROFICIENCY}",
            name="ck_position_competencies_proficiency",
        ),
        CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from",
            name="ck_position_competencies_effective_range",
        ),
        CheckConstraint(
            "display_order IS NULL OR display_order >= 0",
            name="ck_position_competencies_display_order",
        ),
        archive_reason_constraint("position_competencies"),
        tenant_index(
            "position_competencies",
            "position_id",
            "effective_from",
            name="ix_position_competencies_org_pos",
        ),
        tenant_index(
            "position_competencies", "competency_id", name="ix_position_competencies_org_comp"
        ),
    )

    organization_id: Mapped[uuid.UUID] = _organization_column()
    position_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    competency_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    requirement_type: Mapped[str] = mapped_column(
        String(10),
        nullable=False,
        default=CompetencyRequirementType.REQUIRED.value,
        server_default="REQUIRED",
    )
    min_proficiency: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=1, server_default=text("1")
    )
    display_order: Mapped[int | None] = mapped_column(Integer, nullable=True)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)


class PositionStatusHistory(Base, AppendOnlyColumns):
    """Append-only record of every position lifecycle transition (JA §10.7).

    Append-only in the §1.5 sense: no ``row_version``, no ``updated_*``, no archival. A
    correction is a new row, which is what makes the lifecycle auditable at all.
    """

    __tablename__ = "position_status_history"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_position_status_history_org_id"),
        ForeignKeyConstraint(
            ["organization_id", "position_id"],
            ["positions.organization_id", "positions.id"],
            name="fk_position_status_history_position",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "from_status IS NULL OR from_status IN ('DRAFT','OPEN','FROZEN','CLOSED')",
            name="ck_position_status_history_from",
        ),
        _enum_check("to_status", PositionStatus, "ck_position_status_history_to"),
        Index("ix_position_status_history_position", "position_id", "effective_at"),
    )

    organization_id: Mapped[uuid.UUID] = _organization_column()
    position_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(10), nullable=True)
    to_status: Mapped[str] = mapped_column(String(10), nullable=False)
    effective_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=text("CURRENT_TIMESTAMP(6)")
    )
    reason: Mapped[str | None] = mapped_column(String(500), nullable=True)


class EmployeePositionAssignment(Base, StandardColumns):
    """The authoritative occupancy record (JA §10.8).

    Effective-dated with a half-open ``[effective_from, effective_to)`` interval, matching the
    salary and bank conventions. Occupancy, vacancy and every headcount report are derived
    from these rows and nothing else.

    ``active_primary_key`` is the MySQL stand-in for a partial unique index: it holds
    ``employee_id`` exactly while the row is an ``ACTIVE`` ``PRIMARY`` assignment and ``NULL``
    otherwise, so ``UNIQUE (organization_id, active_primary_key)`` enforces "one active
    primary assignment per employee" in the database. A generated column cannot do this
    because the value must come from another column *conditionally*; the service maintains it
    and the unique index is the backstop, the same arrangement ``single_current_key`` uses for
    documents (ARCHITECTURE §7).
    """

    __tablename__ = "employee_position_assignments"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_emp_position_assignments_org_id"),
        UniqueConstraint(
            "employee_id",
            "position_id",
            "effective_from",
            name="uq_emp_position_assignments_emp_pos_start",
        ),
        UniqueConstraint(
            "organization_id", "active_primary_key", name="uq_emp_position_assignments_primary"
        ),
        ForeignKeyConstraint(
            ["organization_id", "employee_id"],
            ["employees.organization_id", "employees.id"],
            name="fk_emp_position_assignments_employee",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "position_id"],
            ["positions.organization_id", "positions.id"],
            name="fk_emp_position_assignments_position",
            ondelete="RESTRICT",
        ),
        CheckConstraint("fte > 0 AND fte <= 1.00", name="ck_emp_position_assignments_fte"),
        _enum_check("assignment_type", AssignmentType, "ck_emp_position_assignments_type"),
        _enum_check("assignment_status", AssignmentStatus, "ck_emp_position_assignments_status"),
        _enum_check("source", AssignmentSource, "ck_emp_position_assignments_source"),
        CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from",
            name="ck_emp_position_assignments_dates",
        ),
        # JA §10.8: an ended assignment must say when and why.
        CheckConstraint(
            "assignment_status <> 'ENDED' OR (effective_to IS NOT NULL AND end_reason IS NOT NULL)",
            name="ck_emp_position_assignments_ended",
        ),
        # The backstop key must agree with the status/type it encodes, so a service bug
        # cannot park a stale key on an ended row and block the employee's next assignment.
        CheckConstraint(
            "(assignment_status = 'ACTIVE' AND assignment_type = 'PRIMARY' "
            "AND active_primary_key = employee_id) OR "
            "((assignment_status <> 'ACTIVE' OR assignment_type <> 'PRIMARY') "
            "AND active_primary_key IS NULL)",
            name="ck_emp_position_assignments_primary_key",
        ),
        archive_reason_constraint("employee_position_assignments"),
        tenant_index(
            "employee_position_assignments",
            "employee_id",
            "assignment_status",
            "effective_from",
            name="ix_emp_pos_assign_org_employee",
        ),
        tenant_index(
            "employee_position_assignments",
            "position_id",
            "assignment_status",
            "effective_from",
            name="ix_emp_pos_assign_org_position",
        ),
        tenant_index(
            "employee_position_assignments",
            "position_id",
            "effective_to",
            name="ix_emp_pos_assign_org_pos_end",
        ),
    )

    organization_id: Mapped[uuid.UUID] = _organization_column()
    employee_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    position_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    assignment_type: Mapped[str] = mapped_column(
        String(10), nullable=False, default=AssignmentType.PRIMARY.value, server_default="PRIMARY"
    )
    assignment_status: Mapped[str] = mapped_column(
        String(10),
        nullable=False,
        default=AssignmentStatus.SCHEDULED.value,
        server_default="SCHEDULED",
    )
    fte: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), nullable=False, default=Decimal("1.00"), server_default=text("1.00")
    )
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    end_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    source: Mapped[str] = mapped_column(
        String(20), nullable=False, default=AssignmentSource.MANUAL.value, server_default="MANUAL"
    )
    active_primary_key: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)


# =====================================================================================
# Headcount
# =====================================================================================


class HeadcountPlan(Base, StandardColumns):
    """A scoped cap on authorized establishment (JA §8.6, §10.10).

    A plan is a *cap*, not an allocation: it does not create positions and does not reserve
    FTE for a department. Every matching ``OPEN``/``FROZEN`` position's authorized capacity
    counts against it, so opening a position must satisfy its own cap and every matching plan.

    ``scope_key`` is a stored generated canonical rendering of the scope columns, which gives
    each exact scope one comparable key. ``UNIQUE (organization_id, scope_key,
    effective_from)`` blocks the same-start overlap; the service blocks any overlap under a
    lock, because MySQL offers no exclusion constraint.

    This table archives through ``plan_status = 'ARCHIVED'`` rather than ``archived_at``
    (JA §10.10), so the ``StandardColumns`` archival columns stay NULL here.
    """

    __tablename__ = "headcount_plans"
    __table_args__ = (
        UniqueConstraint(
            "organization_id", "plan_code_normalized", name="uq_headcount_plans_org_code"
        ),
        UniqueConstraint("organization_id", "id", name="uq_headcount_plans_org_id"),
        UniqueConstraint(
            "organization_id", "scope_key", "effective_from", name="uq_headcount_plans_scope_start"
        ),
        ForeignKeyConstraint(
            ["organization_id", "department_id"],
            ["departments.organization_id", "departments.id"],
            name="fk_headcount_plans_department",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "branch_id"],
            ["branches.organization_id", "branches.id"],
            name="fk_headcount_plans_branch",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "work_location_id"],
            ["work_locations.organization_id", "work_locations.id"],
            name="fk_headcount_plans_work_location",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "job_id"],
            ["jobs.organization_id", "jobs.id"],
            name="fk_headcount_plans_job",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "grade_id"],
            ["grades.organization_id", "grades.id"],
            name="fk_headcount_plans_grade",
            ondelete="RESTRICT",
        ),
        # JA §10.10: exactly one scope. Spelled out per scope type rather than as a sum, so
        # the database rejects both "no scope id" and "two scope ids" for every variant.
        CheckConstraint(
            "(scope_type='ORGANIZATION' AND department_id IS NULL AND branch_id IS NULL "
            "AND work_location_id IS NULL AND job_id IS NULL AND grade_id IS NULL) OR "
            "(scope_type='DEPARTMENT' AND department_id IS NOT NULL AND branch_id IS NULL "
            "AND work_location_id IS NULL AND job_id IS NULL AND grade_id IS NULL) OR "
            "(scope_type='BRANCH' AND department_id IS NULL AND branch_id IS NOT NULL "
            "AND work_location_id IS NULL AND job_id IS NULL AND grade_id IS NULL) OR "
            "(scope_type='WORK_LOCATION' AND department_id IS NULL AND branch_id IS NULL "
            "AND work_location_id IS NOT NULL AND job_id IS NULL AND grade_id IS NULL) OR "
            "(scope_type='JOB' AND department_id IS NULL AND branch_id IS NULL "
            "AND work_location_id IS NULL AND job_id IS NOT NULL AND grade_id IS NULL) OR "
            "(scope_type='GRADE' AND department_id IS NULL AND branch_id IS NULL "
            "AND work_location_id IS NULL AND job_id IS NULL AND grade_id IS NOT NULL)",
            name="ck_headcount_plans_exactly_one_scope",
        ),
        CheckConstraint(
            "authorized_fte >= 0 AND authorized_headcount >= 0",
            name="ck_headcount_plans_non_negative",
        ),
        CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from",
            name="ck_headcount_plans_effective_range",
        ),
        _enum_check("plan_status", HeadcountPlanStatus, "ck_headcount_plans_status"),
        # JA §15.4: an active plan is an approved plan. Approval cannot be implied.
        CheckConstraint(
            "plan_status <> 'ACTIVE' OR (approved_by IS NOT NULL AND approved_at IS NOT NULL)",
            name="ck_headcount_plans_approved",
        ),
        *_code_checks("headcount_plans", "plan_code", name_column="name", name_max=150),
        archive_reason_constraint("headcount_plans"),
        tenant_index(
            "headcount_plans",
            "plan_status",
            "effective_from",
            name="ix_headcount_plans_org_status",
        ),
        tenant_index(
            "headcount_plans", "scope_type", "scope_key", name="ix_headcount_plans_org_scope"
        ),
    )

    organization_id: Mapped[uuid.UUID] = _organization_column()
    plan_code: Mapped[str] = mapped_column(String(30), nullable=False)
    plan_code_normalized: Mapped[str] = mapped_column(
        String(30), Computed("UPPER(plan_code)", persisted=True), nullable=False
    )
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    description: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    scope_type: Mapped[str] = mapped_column(String(20), nullable=False)
    department_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    branch_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    work_location_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    grade_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    scope_key: Mapped[str] = mapped_column(
        String(200),
        Computed(
            "CONCAT(scope_type, ':', "
            "COALESCE(HEX(department_id), ''), ':', "
            "COALESCE(HEX(branch_id), ''), ':', "
            "COALESCE(HEX(work_location_id), ''), ':', "
            "COALESCE(HEX(job_id), ''), ':', "
            "COALESCE(HEX(grade_id), ''))",
            persisted=True,
        ),
        nullable=False,
    )
    authorized_fte: Mapped[Decimal] = mapped_column(Numeric(9, 2), nullable=False)
    authorized_headcount: Mapped[int] = mapped_column(Integer, nullable=False)
    plan_status: Mapped[str] = mapped_column(
        String(10), nullable=False, default=HeadcountPlanStatus.DRAFT.value, server_default="DRAFT"
    )
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    approved_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid7, ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(nullable=True)


class HeadcountChangeRequest(Base, StandardColumns):
    """The maker-checker aggregate for every capacity change (JA §8.7, §10.11).

    Deliberately one dedicated state machine rather than a generic workflow engine, which
    JA §1.3 excludes. Self-approval is blocked in three places: the service, and two database
    CHECKs comparing ``decided_by`` with ``created_by`` and ``submitted_by`` — so a direct
    SQL update cannot approve its own request either (§17.4 uses the same approach for
    salary).

    ``current_*`` is a snapshot taken at creation for history; approval re-reads the live
    values under a lock before applying, so a stale snapshot can never authorize more than
    the request asked for.
    """

    __tablename__ = "headcount_change_requests"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_headcount_requests_org_id"),
        ForeignKeyConstraint(
            ["organization_id", "position_id"],
            ["positions.organization_id", "positions.id"],
            name="fk_headcount_requests_position",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "headcount_plan_id"],
            ["headcount_plans.organization_id", "headcount_plans.id"],
            name="fk_headcount_requests_plan",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "(position_id IS NOT NULL) + (headcount_plan_id IS NOT NULL) = 1",
            name="ck_headcount_requests_one_target",
        ),
        _enum_check("change_type", HeadcountChangeType, "ck_headcount_requests_change_type"),
        _enum_check("request_status", HeadcountRequestStatus, "ck_headcount_requests_status"),
        CheckConstraint(
            "requested_fte >= 0 AND requested_headcount >= 0",
            name="ck_headcount_requests_non_negative",
        ),
        CheckConstraint(
            f"CHAR_LENGTH(reason) >= {HEADCOUNT_REASON_MIN_LENGTH}",
            name="ck_headcount_requests_reason",
        ),
        CheckConstraint(
            "request_status <> 'PENDING' OR "
            "(submitted_at IS NOT NULL AND submitted_by IS NOT NULL)",
            name="ck_headcount_requests_submitted",
        ),
        CheckConstraint(
            "request_status NOT IN ('APPROVED','REJECTED') OR "
            "(decided_by IS NOT NULL AND decided_at IS NOT NULL)",
            name="ck_headcount_requests_decided",
        ),
        CheckConstraint(
            "request_status <> 'REJECTED' OR decision_note IS NOT NULL",
            name="ck_headcount_requests_rejection_note",
        ),
        CheckConstraint(
            "decided_by IS NULL OR created_by IS NULL OR decided_by <> created_by",
            name="ck_headcount_requests_no_self_approval_creator",
        ),
        CheckConstraint(
            "decided_by IS NULL OR submitted_by IS NULL OR decided_by <> submitted_by",
            name="ck_headcount_requests_no_self_approval_submitter",
        ),
        archive_reason_constraint("headcount_change_requests"),
        tenant_index(
            "headcount_change_requests",
            "request_status",
            "created_at",
            name="ix_headcount_requests_org_status",
        ),
        tenant_index(
            "headcount_change_requests",
            "position_id",
            "request_status",
            name="ix_headcount_requests_org_position",
        ),
        tenant_index(
            "headcount_change_requests",
            "headcount_plan_id",
            "request_status",
            name="ix_headcount_requests_org_plan",
        ),
    )

    organization_id: Mapped[uuid.UUID] = _organization_column()
    position_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    headcount_plan_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    change_type: Mapped[str] = mapped_column(String(10), nullable=False)
    current_fte: Mapped[Decimal] = mapped_column(Numeric(9, 2), nullable=False)
    requested_fte: Mapped[Decimal] = mapped_column(Numeric(9, 2), nullable=False)
    current_headcount: Mapped[int] = mapped_column(Integer, nullable=False)
    requested_headcount: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(String(1000), nullable=False)
    request_status: Mapped[str] = mapped_column(
        String(10),
        nullable=False,
        default=HeadcountRequestStatus.DRAFT.value,
        server_default="DRAFT",
    )
    submitted_at: Mapped[datetime | None] = mapped_column(nullable=True)
    submitted_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid7, ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    decided_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid7, ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    decided_at: Mapped[datetime | None] = mapped_column(nullable=True)
    decision_note: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    applied_at: Mapped[datetime | None] = mapped_column(nullable=True)


class HeadcountReservation(Base, StandardColumns):
    """A non-employee commitment against a position's capacity (JA §8.8, §10.12).

    Optional and **disabled by default** (JA-Q-14): until an onboarding or recruitment
    integration exists there is no producer for these rows, and a capacity reducer with no
    producer is a trap. The organization setting ``enable_headcount_reservations`` turns the
    endpoints on.

    A reservation reduces vacancy. It is not an assignment and never becomes one.
    """

    __tablename__ = "headcount_reservations"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_headcount_reservations_org_id"),
        ForeignKeyConstraint(
            ["organization_id", "position_id"],
            ["positions.organization_id", "positions.id"],
            name="fk_headcount_reservations_position",
            ondelete="RESTRICT",
        ),
        _enum_check("source_type", ReservationSourceType, "ck_headcount_reservations_source"),
        _enum_check("reservation_status", ReservationStatus, "ck_headcount_reservations_status"),
        CheckConstraint(
            "reserved_fte > 0 AND reserved_fte <= 1.00", name="ck_headcount_reservations_fte"
        ),
        CheckConstraint(
            "reserved_headcount BETWEEN 1 AND 999", name="ck_headcount_reservations_headcount"
        ),
        CheckConstraint(
            "reservation_status <> 'ACTIVE' OR expires_at IS NOT NULL",
            name="ck_headcount_reservations_expiry_required",
        ),
        CheckConstraint(
            "expires_at IS NULL OR expires_at > effective_from",
            name="ck_headcount_reservations_expiry_after_start",
        ),
        archive_reason_constraint("headcount_reservations"),
        tenant_index(
            "headcount_reservations",
            "position_id",
            "reservation_status",
            "expires_at",
            name="ix_headcount_reservations_org_pos",
        ),
    )

    organization_id: Mapped[uuid.UUID] = _organization_column()
    position_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    source_type: Mapped[str] = mapped_column(String(20), nullable=False)
    source_reference: Mapped[str | None] = mapped_column(String(100), nullable=True)
    """An opaque external reference. JA §10.12: never personal data."""

    reserved_fte: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), nullable=False, default=Decimal("1.00"), server_default=text("1.00")
    )
    reserved_headcount: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=1, server_default=text("1")
    )
    reservation_status: Mapped[str] = mapped_column(
        String(10), nullable=False, default=ReservationStatus.ACTIVE.value, server_default="ACTIVE"
    )
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(nullable=True)
    reason: Mapped[str | None] = mapped_column(String(500), nullable=True)


__all__ = [
    "ACTIVE_ASSIGNMENT_STATUSES",
    "ESTABLISHMENT_POSITION_STATUSES",
    "HEADCOUNT_REASON_MIN_LENGTH",
    "JOB_FAMILY_MAX_DEPTH",
    "MAX_PROFICIENCY",
    "MIN_PROFICIENCY",
    "OCCUPYING_ASSIGNMENT_STATUSES",
    "POSITION_REPORTING_MAX_DEPTH",
    "AssignmentSource",
    "AssignmentStatus",
    "AssignmentType",
    "CatalogueStatus",
    "Competency",
    "CompetencyRequirementType",
    "EmployeePositionAssignment",
    "HeadcountChangeRequest",
    "HeadcountChangeType",
    "HeadcountPlan",
    "HeadcountPlanStatus",
    "HeadcountRequestStatus",
    "HeadcountReservation",
    "HeadcountScopeType",
    "Job",
    "JobCompetency",
    "JobFamily",
    "JobNodeLevel",
    "Position",
    "PositionCompetency",
    "PositionStatus",
    "PositionStatusHistory",
    "PositionWorkMode",
    "ReservationSourceType",
    "ReservationStatus",
]
