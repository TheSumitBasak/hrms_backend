"""Salary reference data: components, versioned templates, assignments, revision history.

Covers §16, §17 and §18.

**Scope boundary.** Salary data is stored for record-keeping. §4.3 forbids computing or
storing net pay, gross totals, sums of components, tax, statutory contributions, gratuity,
leave encashment, arrears, deltas, projections and cost-to-company. The one permitted
calculation is a ``PERCENTAGE`` component's own amount: ``percentage x Basic``, resolved when
an assignment is saved and stored in ``computed_amount``. Nothing else is derived from stored
values.

**Immutability.** A confirmed assignment's values and snapshots never change (INV-5). A
correction does not edit them — it cancels the assignment and inserts a replacement (§18.5),
so history is append-only in effect even though the rows live in a mutable table.

**Snapshots.** Assignments copy the component code, name, category, value type, period and
template identity at the moment of assignment. That is what makes AC-07 and E-14 hold: a later
template edit or component rename cannot reach into history and change what someone was paid.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Computed,
    Date,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.mixins import AppendOnlyColumns, StandardColumns, archive_reason_constraint
from app.db.types import Uuid7, money
from app.db.types import percentage as percentage_column


class ComponentCategory(StrEnum):
    """§16.3 ``component_category``.

    ``EMPLOYER_BENEFIT`` and ``EMPLOYEE_DEDUCTION_REFERENCE`` are labels for grouping in the
    UI. §4.2 permits storing an "employer contribution reference" value as entered; it does
    not permit computing one.
    """

    EARNINGS = "EARNINGS"
    ALLOWANCE = "ALLOWANCE"
    EMPLOYER_BENEFIT = "EMPLOYER_BENEFIT"
    EMPLOYEE_DEDUCTION_REFERENCE = "EMPLOYEE_DEDUCTION_REFERENCE"
    OTHER = "OTHER"


class ComponentType(StrEnum):
    """Whether a component adds to (EARNING) or is taken from (DEDUCTION) the employee's pay.

    A label for grouping and display only; nothing totals or nets the two groups.
    """

    EARNING = "EARNING"
    DEDUCTION = "DEDUCTION"


BASIC_COMPONENT_CODE = "BASIC"
"""The reserved code of the system component every template and assignment carries. A
``PERCENTAGE`` component is a percentage **of this component's amount**."""


class ValueType(StrEnum):
    """§16.3 ``value_type``. Immutable once used in any version or assignment."""

    FIXED_AMOUNT = "FIXED_AMOUNT"
    PERCENTAGE = "PERCENTAGE"
    TEXT = "TEXT"
    BOOLEAN = "BOOLEAN"
    OTHER_CONFIGURED = "OTHER_CONFIGURED"


class AmountPeriod(StrEnum):
    """§16.3 ``amount_period``.

    Required for ``FIXED_AMOUNT`` so a stored figure is never ambiguous between annual and
    monthly — the kind of ambiguity that invites exactly the arithmetic §4.3 forbids.
    """

    ANNUAL = "ANNUAL"
    MONTHLY = "MONTHLY"
    ONE_TIME = "ONE_TIME"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class TemplateVersionStatus(StrEnum):
    """§16.5 ``status``."""

    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    ARCHIVED = "ARCHIVED"


class AssignmentStatus(StrEnum):
    """§17.1 ``assignment_status``, derived from dates per §18.2."""

    DRAFT = "DRAFT"
    SCHEDULED = "SCHEDULED"
    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


CONFIRMED_STATUSES: frozenset[AssignmentStatus] = frozenset(
    {AssignmentStatus.SCHEDULED, AssignmentStatus.ACTIVE, AssignmentStatus.EXPIRED}
)
"""§18.1: these form the employee's salary timeline. DRAFT and CANCELLED do not participate,
which is why INV-1 applies only to this set."""

_CONFIRMED_SQL = ",".join(f"'{status.value}'" for status in sorted(CONFIRMED_STATUSES))


class ApprovalState(StrEnum):
    """§17.1 ``approval_state`` (§17.4 maker-checker)."""

    NOT_REQUIRED = "NOT_REQUIRED"
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class EffectiveToSource(StrEnum):
    """§17.1 ``effective_to_source``, which drives boundary restoration on cancel (§18.4).

    Knowing *why* an interval ends is what lets O2 restore it correctly: a boundary the system
    created to make room for a successor may be given back, while one a user set explicitly
    must not be silently widened.
    """

    NONE = "NONE"
    USER = "USER"
    SUCCESSOR = "SUCCESSOR"
    EMPLOYMENT_END = "EMPLOYMENT_END"


class CancellationType(StrEnum):
    """§17.1 ``cancellation_type``."""

    USER_CANCEL = "USER_CANCEL"
    REPLACED = "REPLACED"
    CORRECTION = "CORRECTION"


class RevisionEventType(StrEnum):
    """§17.5 ``event_type`` — the full lifecycle of a revision."""

    CREATED_DRAFT = "CREATED_DRAFT"
    UPDATED_DRAFT = "UPDATED_DRAFT"
    SUBMITTED = "SUBMITTED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    CONFIRMED = "CONFIRMED"
    BOUNDARY_ADJUSTED = "BOUNDARY_ADJUSTED"
    ACTIVATED = "ACTIVATED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"
    REPLACED = "REPLACED"
    CORRECTED = "CORRECTED"


def _typed_value_check(table_name: str, prefix: str, type_column: str) -> CheckConstraint:
    """The §16.2 typed-value rule: exactly the column matching the type is populated.

    "CHECK: at most the single column matching value_type is non-null; all others null." A
    percentage stored in the amount column would be a figure that looks like money and is not,
    which is precisely the confusion §4.3 exists to prevent.
    """
    amount = f"{prefix}_amount"
    percentage = f"{prefix}_percentage"
    text_value = f"{prefix}_text"
    boolean = f"{prefix}_boolean"
    return CheckConstraint(
        f"({type_column} = 'FIXED_AMOUNT' AND {percentage} IS NULL "
        f" AND {text_value} IS NULL AND {boolean} IS NULL) "
        f"OR ({type_column} = 'PERCENTAGE' AND {amount} IS NULL "
        f" AND {text_value} IS NULL AND {boolean} IS NULL) "
        f"OR ({type_column} IN ('TEXT','OTHER_CONFIGURED') AND {amount} IS NULL "
        f" AND {percentage} IS NULL AND {boolean} IS NULL) "
        f"OR ({type_column} = 'BOOLEAN' AND {amount} IS NULL "
        f" AND {percentage} IS NULL AND {text_value} IS NULL)",
        name=f"ck_{table_name}_{prefix}_typed_value",
    )


class SalaryComponent(Base, StandardColumns):
    """An organization-defined compensation field (§16.3).

    No component list is hard-coded (NFR-15): §16.3 offers seed suggestions an administrator
    may delete before use. ``value_type`` becomes immutable once the component appears in any
    template version or assignment, because snapshots elsewhere have already recorded it.
    """

    __tablename__ = "salary_components"
    __table_args__ = (
        UniqueConstraint(
            "organization_id", "component_code_normalized", name="uq_salary_components_org_code"
        ),
        UniqueConstraint("organization_id", "id", name="uq_salary_components_org_id"),
        CheckConstraint(
            "component_code REGEXP '^[A-Z0-9][A-Z0-9_./-]{0,29}$'",
            name="ck_salary_components_code_pattern",
        ),
        CheckConstraint(
            "component_category IN ("
            + ",".join(f"'{member.value}'" for member in ComponentCategory)
            + ")",
            name="ck_salary_components_category",
        ),
        CheckConstraint(
            "value_type IN (" + ",".join(f"'{member.value}'" for member in ValueType) + ")",
            name="ck_salary_components_value_type",
        ),
        # §16.3: a fixed amount must declare its period; anything else must not pretend to have
        # one beyond NOT_APPLICABLE.
        CheckConstraint(
            "(value_type = 'FIXED_AMOUNT' AND amount_period IN ('ANNUAL','MONTHLY','ONE_TIME')) "
            "OR (value_type <> 'FIXED_AMOUNT' AND amount_period = 'NOT_APPLICABLE')",
            name="ck_salary_components_amount_period",
        ),
        # §16.3: a currency is required for a fixed amount.
        CheckConstraint(
            "value_type <> 'FIXED_AMOUNT' OR currency IS NOT NULL",
            name="ck_salary_components_currency_required",
        ),
        # §16.3: a percentage reference label is a display string for percentages only.
        CheckConstraint(
            "percentage_reference_label IS NULL OR value_type = 'PERCENTAGE'",
            name="ck_salary_components_percentage_label_only_for_percentage",
        ),
        # §16.3: OTHER_CONFIGURED is validated by a regex, which must therefore exist.
        CheckConstraint(
            "(value_type = 'OTHER_CONFIGURED' AND validation_regex IS NOT NULL) "
            "OR (value_type <> 'OTHER_CONFIGURED')",
            name="ck_salary_components_validation_regex",
        ),
        CheckConstraint(
            "component_type IN ('EARNING','DEDUCTION')", name="ck_salary_components_type"
        ),
        CheckConstraint("display_order >= 0", name="ck_salary_components_display_order"),
        CheckConstraint(
            "default_value_amount IS NULL OR default_value_amount >= 0",
            name="ck_salary_components_default_amount_non_negative",
        ),
        CheckConstraint(
            "default_value_percentage IS NULL "
            "OR (default_value_percentage >= 0 AND default_value_percentage <= 100)",
            name="ck_salary_components_default_percentage_range",
        ),
        _typed_value_check("salary_components", "default_value", "value_type"),
        archive_reason_constraint("salary_components"),
        Index("ix_salary_components_org_status", "organization_id", "status", "display_order"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid7, ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    component_code: Mapped[str] = mapped_column(String(30), nullable=False)
    component_code_normalized: Mapped[str] = mapped_column(
        String(30), Computed("UPPER(component_code)", persisted=True), nullable=False
    )
    component_name: Mapped[str] = mapped_column(String(100), nullable=False)
    """The internal name administrators use to tell components apart. Renaming is allowed and
    does not affect snapshots (§16.3, E-14)."""

    display_name: Mapped[str] = mapped_column(String(100), nullable=False)
    """What the employee sees (payslip, salary view)."""

    component_type: Mapped[str] = mapped_column(
        String(10), nullable=False, default=ComponentType.EARNING.value, server_default="EARNING"
    )
    is_system: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    """True only for Basic: it is seeded with the organization and can be neither deactivated
    nor retyped."""

    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    component_category: Mapped[str] = mapped_column(String(40), nullable=False)
    value_type: Mapped[str] = mapped_column(String(20), nullable=False)
    amount_period: Mapped[str] = mapped_column(
        String(20), nullable=False, default=AmountPeriod.NOT_APPLICABLE
    )
    percentage_reference_label: Mapped[str | None] = mapped_column(String(100), nullable=True)
    """§16.3: a display label such as "% of Basic". **Not** a computation instruction — §4.3
    forbids applying the percentage to anything."""

    validation_regex: Mapped[str | None] = mapped_column(String(200), nullable=True)

    # §16.2 typed value group, for the component's default.
    default_value_amount: Mapped[Decimal | None] = mapped_column(money(), nullable=True)
    default_value_percentage: Mapped[Decimal | None] = mapped_column(
        percentage_column(), nullable=True
    )
    default_value_text: Mapped[str | None] = mapped_column(String(500), nullable=True)
    default_value_boolean: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    is_required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    is_editable_per_employee: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("1")
    )
    """When false, no employee override is possible in any template (§16.3), which forces
    ``is_overridable`` false on every mapping."""

    display_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="ACTIVE")


class SalaryTemplate(Base, StandardColumns):
    """A reusable salary structure header (§16.4). Versions carry the actual mappings."""

    __tablename__ = "salary_templates"
    __table_args__ = (
        UniqueConstraint(
            "organization_id", "template_code_normalized", name="uq_salary_templates_org_code"
        ),
        UniqueConstraint("organization_id", "id", name="uq_salary_templates_org_id"),
        CheckConstraint(
            "template_code REGEXP '^[A-Z0-9][A-Z0-9_./-]{0,29}$'",
            name="ck_salary_templates_code_pattern",
        ),
        archive_reason_constraint("salary_templates"),
        Index("ix_salary_templates_org", "organization_id", "is_archived"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid7, ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    template_code: Mapped[str] = mapped_column(String(30), nullable=False)
    template_code_normalized: Mapped[str] = mapped_column(
        String(30), Computed("UPPER(template_code)", persisted=True), nullable=False
    )
    template_name: Mapped[str] = mapped_column(String(150), nullable=False)
    description: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    is_archived: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    """Archiving archives all versions (§16.4). Archived templates cannot be newly assigned
    (``422 TEMPLATE_ARCHIVED``) but stay readable in history."""


class SalaryTemplateVersion(Base, StandardColumns):
    """One immutable-once-published version of a template (§16.5).

    DRAFT versions are fully editable; an ACTIVE version is immutable except for
    ``effective_to``, which publishing a later version sets, and archival (§16.7). "Editing" an
    active template therefore means creating a new draft version that copies its mappings.

    Versions of one template never overlap (C-10). PostgreSQL uses an exclusion constraint;
    here the unique ``(template_id, version_number)`` plus a publish-time check under lock plus
    the nightly job play that role (ARCHITECTURE §7).
    """

    __tablename__ = "salary_template_versions"
    __table_args__ = (
        UniqueConstraint(
            "template_id", "version_number", name="uq_salary_template_versions_template_version"
        ),
        UniqueConstraint("organization_id", "id", name="uq_salary_template_versions_org_id"),
        ForeignKeyConstraint(
            ["organization_id", "template_id"],
            ["salary_templates.organization_id", "salary_templates.id"],
            name="fk_salary_template_versions_template",
            ondelete="RESTRICT",
        ),
        CheckConstraint("version_number >= 1", name="ck_salary_template_versions_version_number"),
        CheckConstraint(
            "status IN ('DRAFT','ACTIVE','ARCHIVED')", name="ck_salary_template_versions_status"
        ),
        CheckConstraint(
            "effective_to IS NULL OR effective_from IS NULL OR effective_to > effective_from",
            name="ck_salary_template_versions_effective_interval",
        ),
        # §16.5: publishing requires an effective date, and version 2 onwards requires a note
        # saying what changed.
        CheckConstraint(
            "status = 'DRAFT' OR effective_from IS NOT NULL",
            name="ck_salary_template_versions_published_requires_effective_from",
        ),
        CheckConstraint(
            "status = 'DRAFT' OR version_number = 1 OR change_note IS NOT NULL",
            name="ck_salary_template_versions_change_note_required",
        ),
        archive_reason_constraint("salary_template_versions"),
        Index("ix_salary_template_versions_lookup", "template_id", "status", "effective_from"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    template_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    """All ``FIXED_AMOUNT`` mappings in this version must use it (§16.5)."""

    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=TemplateVersionStatus.DRAFT
    )
    effective_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    """Exclusive; set automatically when a later version is published (§16.7)."""

    change_note: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    published_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid7, ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    published_at: Mapped[datetime | None] = mapped_column(nullable=True)


class SalaryTemplateComponent(Base, StandardColumns):
    """A component mapping inside one template version (§16.6)."""

    __tablename__ = "salary_template_components"
    __table_args__ = (
        # §16.7: duplicate component mappings within one version are prohibited.
        UniqueConstraint(
            "template_version_id",
            "component_id",
            name="uq_salary_template_components_version_component",
        ),
        ForeignKeyConstraint(
            ["organization_id", "component_id"],
            ["salary_components.organization_id", "salary_components.id"],
            name="fk_salary_template_components_component",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "template_version_id"],
            ["salary_template_versions.organization_id", "salary_template_versions.id"],
            name="fk_salary_template_components_version",
            ondelete="RESTRICT",
        ),
        CheckConstraint("display_order >= 0", name="ck_salary_template_components_display_order"),
        CheckConstraint(
            "default_value_amount IS NULL OR default_value_amount >= 0",
            name="ck_salary_template_components_amount_non_negative",
        ),
        CheckConstraint(
            "default_value_percentage IS NULL "
            "OR (default_value_percentage >= 0 AND default_value_percentage <= 100)",
            name="ck_salary_template_components_percentage_range",
        ),
        CheckConstraint(
            "effective_to IS NULL OR effective_from IS NULL OR effective_to > effective_from",
            name="ck_salary_template_components_effective_interval",
        ),
        archive_reason_constraint("salary_template_components"),
        Index("ix_salary_template_components_version", "template_version_id", "display_order"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    template_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    """§16.6 calls this denormalised for querying; it is kept consistent with the version's
    own ``template_id`` by the service."""

    template_version_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    component_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)

    default_value_amount: Mapped[Decimal | None] = mapped_column(money(), nullable=True)
    default_value_percentage: Mapped[Decimal | None] = mapped_column(
        percentage_column(), nullable=True
    )
    default_value_text: Mapped[str | None] = mapped_column(String(500), nullable=True)
    default_value_boolean: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    is_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_overridable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    """Forced false when the component is not editable per employee (§16.6)."""

    display_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    effective_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    """§16.6: a mapping applies only to assignments whose ``effective_from`` falls in its own
    half-open interval, which lets one version phase a component in or out."""


class EmployeeSalaryAssignment(Base, StandardColumns):
    """One effective-dated salary structure for one employee (§17.1).

    The confirmed assignments of an employee form a timeline whose intervals are pairwise
    disjoint (INV-1). MySQL cannot express that declaratively, so the protection is: an
    employee row lock taken first (fixing the lock order and so avoiding deadlocks), an
    in-transaction overlap query, the same-start unique constraint below, and the nightly
    integrity job — exactly the arrangement §18.8 permits.
    """

    __tablename__ = "employee_salary_assignments"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_employee_salary_assignments_org_id"),
        # §18.8: "a unique constraint on a generated column (employee_id, confirmed_from)
        # preventing same-start duplicates". NULL for DRAFT and CANCELLED rows, which are not
        # part of the timeline (ADR-003).
        UniqueConstraint(
            "employee_id", "confirmed_from", name="uq_employee_salary_assignments_confirmed_from"
        ),
        ForeignKeyConstraint(
            ["organization_id", "employee_id"],
            ["employees.organization_id", "employees.id"],
            name="fk_employee_salary_assignments_employee",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "template_version_id"],
            ["salary_template_versions.organization_id", "salary_template_versions.id"],
            name="fk_employee_salary_assignments_template_version",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            f"assignment_status IN ('DRAFT',{_CONFIRMED_SQL},'CANCELLED')",
            name="ck_employee_salary_assignments_status",
        ),
        CheckConstraint(
            "approval_state IN ('NOT_REQUIRED','PENDING','APPROVED','REJECTED')",
            name="ck_employee_salary_assignments_approval_state",
        ),
        CheckConstraint(
            "effective_to_source IN ('NONE','USER','SUCCESSOR','EMPLOYMENT_END')",
            name="ck_employee_salary_assignments_effective_to_source",
        ),
        CheckConstraint(  # INV-2
            "effective_to IS NULL OR effective_to > effective_from",
            name="ck_employee_salary_assignments_inv2_interval",
        ),
        # §17.1: a template assignment must record which version and its snapshots; a fully
        # custom assignment has no template at all.
        CheckConstraint(
            "(template_id IS NULL AND template_version_id IS NULL) "
            "OR (template_id IS NOT NULL AND template_version_id IS NOT NULL "
            "    AND template_version_number IS NOT NULL "
            "    AND template_code_snapshot IS NOT NULL)",
            name="ck_employee_salary_assignments_template_snapshot",
        ),
        # §17.1: a backdated confirmation must say why.
        CheckConstraint(
            "is_backdated = 0 OR (backdate_reason IS NOT NULL "
            "AND CHAR_LENGTH(backdate_reason) >= 10)",
            name="ck_employee_salary_assignments_backdate_reason",
        ),
        CheckConstraint(
            "cancellation_type IS NULL "
            "OR cancellation_type IN ('USER_CANCEL','REPLACED','CORRECTION')",
            name="ck_employee_salary_assignments_cancellation_type",
        ),
        # §17.1: a cancellation must record its reason and type.
        CheckConstraint(
            "assignment_status <> 'CANCELLED' OR ("
            "cancellation_type IS NOT NULL AND cancellation_reason IS NOT NULL)",
            name="ck_employee_salary_assignments_cancellation_fields",
        ),
        # §17.1 / §17.4: maker-checker. An approver may be neither the creator nor the
        # submitter, enforced in the database as well as the service because self-approval
        # defeats the entire control.
        CheckConstraint(
            "approved_by IS NULL OR ("
            "approved_by <> created_by "
            "AND (submitted_by IS NULL OR approved_by <> submitted_by))",
            name="ck_employee_salary_assignments_no_self_approval",
        ),
        # C-13 / §6.3: the referenced document must belong to the same employee.
        ForeignKeyConstraint(
            ["employee_id", "revision_letter_document_id"],
            ["employee_documents.employee_id", "employee_documents.id"],
            name="fk_salary_assignments_revision_letter",
            ondelete="RESTRICT",
            use_alter=True,
        ),
        archive_reason_constraint("employee_salary_assignments"),
        Index(
            "ix_employee_salary_assignments_timeline",
            "organization_id",
            "employee_id",
            "effective_from",
        ),
        Index(
            "ix_employee_salary_assignments_status",
            "organization_id",
            "assignment_status",
            "effective_from",
        ),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    employee_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)

    template_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    """Null means a fully custom structure, which needs ``salary.custom_component`` and the
    organization's ``allow_custom_salary_components`` (§17.1)."""

    template_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    template_version_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    template_code_snapshot: Mapped[str | None] = mapped_column(String(30), nullable=True)
    template_name_snapshot: Mapped[str | None] = mapped_column(String(150), nullable=True)
    """Snapshots (``[H]``): AC-07 requires a later template change to leave this untouched."""

    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    assignment_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=AssignmentStatus.DRAFT
    )
    """Stored for querying, but §18.2 requires every "current salary" read to use the date
    predicate instead, so a scheduled revision becomes current at midnight even if the refresh
    job is late (E-12, AC-09)."""

    approval_state: Mapped[str] = mapped_column(
        String(20), nullable=False, default=ApprovalState.NOT_REQUIRED
    )

    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    effective_to_source: Mapped[str] = mapped_column(
        String(20), nullable=False, default=EffectiveToSource.NONE
    )

    confirmed_from: Mapped[date | None] = mapped_column(
        Date,
        Computed(
            f"IF(assignment_status IN ({_CONFIRMED_SQL}), effective_from, NULL)", persisted=True
        ),
        nullable=True,
    )

    revision_reason: Mapped[str] = mapped_column(String(50), nullable=False)
    revision_remarks: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    revision_letter_document_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)

    is_backdated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    backdate_reason: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    predecessor_assignment_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    predecessor_original_effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    """The predecessor's end before truncation (``[H]``), so §18.3 step 7 is explainable and
    §18.4 can restore it."""

    replaces_assignment_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    superseded_by_assignment_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)

    cancellation_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    cancelled_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid7, ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    cancelled_at: Mapped[datetime | None] = mapped_column(nullable=True)
    cancellation_reason: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    submitted_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid7, ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    submitted_at: Mapped[datetime | None] = mapped_column(nullable=True)
    approved_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid7, ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    confirmed_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid7, ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    """The moment it entered the timeline (§17.1), which is also the secondary sort key for
    history display (§17.5)."""

    @property
    def is_confirmed(self) -> bool:
        return self.assignment_status in {status.value for status in CONFIRMED_STATUSES}


class EmployeeSalaryComponent(Base, StandardColumns):
    """One component value within an assignment (§17.2).

    Every descriptive field is a snapshot taken at assignment time. A custom component has no
    ``component_id`` and carries its own code, which must not collide with an existing
    organization component code (§17.2).
    """

    __tablename__ = "employee_salary_components"
    __table_args__ = (
        UniqueConstraint(
            "salary_assignment_id",
            "component_code_snapshot",
            name="uq_employee_salary_components_assignment_code",
        ),
        ForeignKeyConstraint(
            ["salary_assignment_id"],
            ["employee_salary_assignments.id"],
            name="fk_employee_salary_components_assignment",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "value_type IN (" + ",".join(f"'{member.value}'" for member in ValueType) + ")",
            name="ck_employee_salary_components_value_type",
        ),
        CheckConstraint(
            "value_amount IS NULL OR value_amount >= 0",
            name="ck_employee_salary_components_amount_non_negative",
        ),
        CheckConstraint(
            "value_percentage IS NULL OR (value_percentage >= 0 AND value_percentage <= 100)",
            name="ck_employee_salary_components_percentage_range",
        ),
        _typed_value_check("employee_salary_components", "value", "value_type"),
        # §17.1 A-11: an override of a component that had a template default must say why.
        CheckConstraint(
            "is_overridden = 0 OR template_default_present = 0 "
            "OR (override_reason IS NOT NULL AND CHAR_LENGTH(override_reason) >= 5)",
            name="ck_employee_salary_components_override_reason",
        ),
        # §17.2: a custom component has no component_id; a catalogue one does.
        CheckConstraint(
            "(is_custom = 1 AND component_id IS NULL) "
            "OR (is_custom = 0 AND component_id IS NOT NULL)",
            name="ck_employee_salary_components_custom_consistency",
        ),
        CheckConstraint(
            "value_type <> 'FIXED_AMOUNT' OR currency IS NOT NULL",
            name="ck_employee_salary_components_currency_required",
        ),
        CheckConstraint(
            "component_type_snapshot IN ('EARNING','DEDUCTION')",
            name="ck_employee_salary_components_type",
        ),
        archive_reason_constraint("employee_salary_components"),
        Index(
            "ix_employee_salary_components_assignment",
            "salary_assignment_id",
            "display_order_snapshot",
        ),
    )

    salary_assignment_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    component_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)

    component_code_snapshot: Mapped[str] = mapped_column(String(30), nullable=False)
    component_name_snapshot: Mapped[str] = mapped_column(String(100), nullable=False)
    display_name_snapshot: Mapped[str] = mapped_column(String(100), nullable=False)
    component_type_snapshot: Mapped[str] = mapped_column(
        String(10), nullable=False, default=ComponentType.EARNING.value, server_default="EARNING"
    )
    component_category_snapshot: Mapped[str] = mapped_column(String(40), nullable=False)
    value_type: Mapped[str] = mapped_column(String(20), nullable=False)
    amount_period_snapshot: Mapped[str | None] = mapped_column(String(20), nullable=True)

    # §16.2 typed value group: the employee's value.
    value_amount: Mapped[Decimal | None] = mapped_column(money(), nullable=True)
    value_percentage: Mapped[Decimal | None] = mapped_column(percentage_column(), nullable=True)
    """For ``PERCENTAGE``: the percentage **of Basic**."""

    computed_amount: Mapped[Decimal | None] = mapped_column(money(), nullable=True)
    """For ``PERCENTAGE``: ``value_percentage x Basic`` frozen at save time, so a later change
    to anything else cannot move it. Null for every other value type."""

    value_text: Mapped[str | None] = mapped_column(String(500), nullable=True)
    value_boolean: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    # The template default at assignment time, for override display (§17.2).
    template_default_amount: Mapped[Decimal | None] = mapped_column(money(), nullable=True)
    template_default_percentage: Mapped[Decimal | None] = mapped_column(
        percentage_column(), nullable=True
    )
    template_default_text: Mapped[str | None] = mapped_column(String(500), nullable=True)
    template_default_boolean: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    template_default_present: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    """Whether the mapping had a default at all. Needed because "no default" and "a default of
    zero" require different override handling, and a null typed value cannot distinguish them."""

    currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    is_overridden: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    override_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    is_custom: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    display_order_snapshot: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class SalaryRevisionHistory(Base, AppendOnlyColumns):
    """An append-only record of every salary revision lifecycle event (§17.5)."""

    __tablename__ = "salary_revision_history"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_salary_revision_history_org_id"),
        ForeignKeyConstraint(
            ["organization_id", "employee_id"],
            ["employees.organization_id", "employees.id"],
            name="fk_salary_revision_history_employee",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["salary_assignment_id"],
            ["employee_salary_assignments.id"],
            name="fk_salary_revision_history_assignment",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "event_type IN (" + ",".join(f"'{member.value}'" for member in RevisionEventType) + ")",
            name="ck_salary_revision_history_event_type",
        ),
        Index(
            "ix_salary_revision_history_employee_occurred",
            "organization_id",
            "employee_id",
            "occurred_at",
        ),
        Index("ix_salary_revision_history_assignment", "salary_assignment_id"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    employee_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    salary_assignment_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    related_assignment_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    """A predecessor or replacement, depending on the event."""

    event_type: Mapped[str] = mapped_column(String(30), nullable=False)
    effective_from_snapshot: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to_before: Mapped[date | None] = mapped_column(Date, nullable=True)
    effective_to_after: Mapped[date | None] = mapped_column(Date, nullable=True)
    """Both set on a ``BOUNDARY_ADJUSTED`` event, so a timeline change is fully explainable."""

    reason: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    component_changes: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True)
    """§17.5: an array of ``{component_code, change_type, previous_value, new_value,
    value_type}`` compared with the predecessor. A **diff for display, with no arithmetic** —
    §4.3 and §29 Q-07 forbid showing differences between amounts."""

    actor_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid7, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    occurred_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=text("CURRENT_TIMESTAMP(6)")
    )
