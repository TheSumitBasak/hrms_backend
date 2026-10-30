"""Leave management and background job runs (addendum §6, §14.2).

MySQL notes (addendum §8.7 gives PostgreSQL definitions): a partial unique index is a UNIQUE
index over a stored generated column that is NULL outside the predicate; exclusion constraints
on date ranges cannot exist, so non-overlap of policy versions is enforced by the service under
a row lock on the parent policy.
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
    Integer,
    Numeric,
    SmallInteger,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.mixins import (
    AppendOnlyColumns,
    StandardColumns,
    archive_reason_constraint,
    tenant_index,
)
from app.db.types import UtcDateTime, Uuid7
from app.models.attendance import CODE_PATTERN_SQL


class LeaveCategory(StrEnum):
    STANDARD = "STANDARD"
    OPTIONAL_HOLIDAY = "OPTIONAL_HOLIDAY"


class PolicyVersionStatus(StrEnum):
    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    ARCHIVED = "ARCHIVED"


class TransactionType(StrEnum):
    OPENING_BALANCE = "OPENING_BALANCE"
    ALLOCATION = "ALLOCATION"
    ACCRUAL = "ACCRUAL"
    CONSUMPTION = "CONSUMPTION"
    CONSUMPTION_REVERSAL = "CONSUMPTION_REVERSAL"
    CARRY_FORWARD_OUT = "CARRY_FORWARD_OUT"
    CARRY_FORWARD_IN = "CARRY_FORWARD_IN"
    LAPSE = "LAPSE"
    NEGATIVE_BALANCE_WRITE_OFF = "NEGATIVE_BALANCE_WRITE_OFF"
    ADJUSTMENT_CREDIT = "ADJUSTMENT_CREDIT"
    ADJUSTMENT_DEBIT = "ADJUSTMENT_DEBIT"
    REVERSAL = "REVERSAL"


class ApplicationStatus(StrEnum):
    SUBMITTED = "SUBMITTED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    WITHDRAWN = "WITHDRAWN"
    CANCELLED = "CANCELLED"


class ApplicationAction(StrEnum):
    SUBMITTED = "SUBMITTED"
    APPROVED_LEVEL_1 = "APPROVED_LEVEL_1"
    APPROVED_FINAL = "APPROVED_FINAL"
    OVERRIDE_APPROVED = "OVERRIDE_APPROVED"
    REJECTED = "REJECTED"
    WITHDRAWN = "WITHDRAWN"
    CANCELLED = "CANCELLED"
    PARTIALLY_CANCELLED = "PARTIALLY_CANCELLED"
    SYSTEM_CANCELLED = "SYSTEM_CANCELLED"
    REASSIGNED = "REASSIGNED"
    DURATION_RECALCULATED = "DURATION_RECALCULATED"
    ADMIN_RECALCULATED = "ADMIN_RECALCULATED"


class JobCode(StrEnum):
    ATTENDANCE_DAY_CLOSE = "ATTENDANCE_DAY_CLOSE"
    ATTENDANCE_AUTO_FINALIZE = "ATTENDANCE_AUTO_FINALIZE"
    ATTENDANCE_RECALCULATION = "ATTENDANCE_RECALCULATION"
    LEAVE_YEAR_END = "LEAVE_YEAR_END"
    LEAVE_ACCRUAL = "LEAVE_ACCRUAL"


def _in(column: str, enum: type[StrEnum]) -> str:
    return f"{column} IN ({','.join(repr(member.value) for member in enum)})"


def _org_fk(table: str, column: str, name: str, **kwargs: Any) -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["organization_id", column],
        [f"{table}.organization_id", f"{table}.id"],
        name=name,
        ondelete="RESTRICT",
        **kwargs,
    )


def _organization_column() -> Mapped[uuid.UUID]:
    return mapped_column(
        Uuid7, ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False, index=True
    )


def _days(*, nullable: bool = False, precision: int = 7) -> Mapped[Any]:
    return mapped_column(Numeric(precision, 2), nullable=nullable)


_HALF_STEP = "MOD({column} * 100, 50) = 0"


class LeaveType(Base, StandardColumns):
    __tablename__ = "leave_types"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_leave_types_org_id"),
        UniqueConstraint(
            "organization_id", "leave_type_code_normalized", name="uq_leave_types_code"
        ),
        CheckConstraint(f"leave_type_code REGEXP {CODE_PATTERN_SQL}", name="ck_leave_types_code"),
        CheckConstraint(
            "CHAR_LENGTH(leave_type_name) BETWEEN 2 AND 100", name="ck_leave_types_name"
        ),
        CheckConstraint(_in("leave_category", LeaveCategory), name="ck_leave_types_category"),
        CheckConstraint(
            "paid_classification IN ('PAID','UNPAID','NOT_SPECIFIED')", name="ck_leave_types_paid"
        ),
        CheckConstraint("status IN ('ACTIVE','INACTIVE')", name="ck_leave_types_status"),
        CheckConstraint("display_order >= 0", name="ck_leave_types_order"),
        CheckConstraint(
            "leave_category <> 'OPTIONAL_HOLIDAY' OR tracks_balance = 1",
            name="ck_leave_types_optional",
        ),
        archive_reason_constraint("leave_types"),
        tenant_index("leave_types", "status", name="ix_leave_types_org_status"),
    )

    organization_id: Mapped[uuid.UUID] = _organization_column()
    leave_type_code: Mapped[str] = mapped_column(String(30), nullable=False)
    leave_type_code_normalized: Mapped[str] = mapped_column(
        String(30), Computed("UPPER(leave_type_code)", persisted=True), nullable=False
    )
    leave_type_name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    leave_category: Mapped[str] = mapped_column(String(20), nullable=False, default="STANDARD")
    paid_classification: Mapped[str] = mapped_column(
        String(15), nullable=False, default="NOT_SPECIFIED"
    )
    tracks_balance: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    show_type_in_team_calendar: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    encashment_eligible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    display_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="ACTIVE")


class LeavePolicy(Base, StandardColumns):
    __tablename__ = "leave_policies"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_leave_policies_org_id"),
        UniqueConstraint(
            "organization_id", "policy_code_normalized", name="uq_leave_policies_code"
        ),
        _org_fk("leave_types", "leave_type_id", "fk_leave_policies_type"),
        CheckConstraint(f"policy_code REGEXP {CODE_PATTERN_SQL}", name="ck_leave_policies_code"),
        CheckConstraint("status IN ('ACTIVE','INACTIVE')", name="ck_leave_policies_status"),
        archive_reason_constraint("leave_policies"),
        tenant_index("leave_policies", "leave_type_id", "status", name="ix_leave_policies_type"),
    )

    organization_id: Mapped[uuid.UUID] = _organization_column()
    leave_type_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    policy_code: Mapped[str] = mapped_column(String(30), nullable=False)
    policy_code_normalized: Mapped[str] = mapped_column(
        String(30), Computed("UPPER(policy_code)", persisted=True), nullable=False
    )
    policy_name: Mapped[str] = mapped_column(String(150), nullable=False)
    description: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="ACTIVE")


_APPLIES = (
    ("branch", "branches"),
    ("department", "departments"),
    ("designation", "designations"),
    ("grade", "grades"),
    ("employment_type", "employment_types"),
    ("employee_category", "employee_categories"),
)
_SPECIFICITY_SQL = " + ".join(
    [f"(applies_{name}_id IS NOT NULL)" for name, _ in _APPLIES] + ["(applies_gender IS NOT NULL)"]
)


class LeavePolicyVersion(Base, StandardColumns):
    """Versioned rules of a policy (§6.3). Fields mirror the addendum column for column."""

    __tablename__ = "leave_policy_versions"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_leave_policy_versions_org_id"),
        UniqueConstraint("policy_id", "version_number", name="uq_leave_policy_versions_number"),
        # At most one DRAFT per policy (LEAVE_POLICY_DRAFT_EXISTS), emulating a partial unique.
        UniqueConstraint("policy_id", "draft_key", name="uq_leave_policy_versions_one_draft"),
        _org_fk("leave_policies", "policy_id", "fk_leave_policy_versions_policy"),
        _org_fk("leave_types", "leave_type_id", "fk_leave_policy_versions_type"),
        *(
            _org_fk(table, f"applies_{name}_id", f"fk_leave_policy_versions_{name}")
            for name, table in _APPLIES
        ),
        CheckConstraint(_in("status", PolicyVersionStatus), name="ck_leave_policy_versions_status"),
        CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from",
            name="ck_leave_policy_versions_range",
        ),
        CheckConstraint(
            "status = 'DRAFT' OR effective_from IS NOT NULL",
            name="ck_leave_policy_versions_published",
        ),
        CheckConstraint("priority BETWEEN 1 AND 999", name="ck_leave_policy_versions_priority"),
        CheckConstraint(
            "allocation_method IN ('NONE','ANNUAL_UPFRONT','PERIODIC_ACCRUAL')",
            name="ck_leave_policy_versions_method",
        ),
        CheckConstraint(
            "status = 'DRAFT' OR allocation_method = 'NONE' OR "
            "(annual_entitlement_days > 0 AND annual_entitlement_days <= 366 "
            "AND joining_cutoff_day BETWEEN 1 AND 31)",
            name="ck_leave_policy_versions_entitlement",
        ),
        CheckConstraint(
            "status = 'DRAFT' OR (allocation_method = 'PERIODIC_ACCRUAL') = "
            "(accrual_frequency IS NOT NULL AND accrual_timing IS NOT NULL)",
            name="ck_leave_policy_versions_accrual",
        ),
        CheckConstraint(
            "accrual_frequency IS NULL OR accrual_frequency IN "
            "('MONTHLY','QUARTERLY','HALF_YEARLY')",
            name="ck_leave_policy_versions_frequency",
        ),
        CheckConstraint(
            "accrual_timing IS NULL OR accrual_timing IN ('PERIOD_START','PERIOD_END')",
            name="ck_leave_policy_versions_timing",
        ),
        CheckConstraint(
            "upfront_proration IS NULL OR upfront_proration IN ('NONE','MONTHLY')",
            name="ck_leave_policy_versions_proration",
        ),
        CheckConstraint(
            "rounding_mode IN ('TWO_DECIMALS','NEAREST_HALF_DAY','DOWN_TO_HALF_DAY')",
            name="ck_leave_policy_versions_rounding",
        ),
        CheckConstraint(
            "negative_balance_year_end IN ('CARRY_FORWARD','WRITE_OFF')",
            name="ck_leave_policy_versions_negative_year_end",
        ),
        CheckConstraint(
            "approval_chain IN ('MANAGER_ONLY','HR_ONLY','MANAGER_THEN_HR')",
            name="ck_leave_policy_versions_chain",
        ),
        CheckConstraint(
            "max_balance_days IS NULL OR max_balance_days > 0", name="ck_leave_policy_versions_cap"
        ),
        CheckConstraint(
            "NOT carry_forward_enabled OR max_carry_forward_days >= 0",
            name="ck_leave_policy_versions_carry",
        ),
        CheckConstraint(
            "NOT allow_negative_balance OR max_negative_balance_days > 0",
            name="ck_leave_policy_versions_negative",
        ),
        CheckConstraint(
            "min_days_per_application IS NULL OR max_days_per_application IS NULL "
            "OR max_days_per_application >= min_days_per_application",
            name="ck_leave_policy_versions_min_max",
        ),
        CheckConstraint(
            "max_backdate_days BETWEEN 0 AND 365", name="ck_leave_policy_versions_backdate"
        ),
        CheckConstraint(
            "max_future_days BETWEEN 0 AND 730", name="ck_leave_policy_versions_future"
        ),
        tenant_index(
            "leave_policy_versions",
            "leave_type_id",
            "status",
            "effective_from",
            name="ix_leave_policy_versions_resolution",
        ),
    )

    organization_id: Mapped[uuid.UUID] = _organization_column()
    policy_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    leave_type_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="DRAFT")
    draft_key: Mapped[int | None] = mapped_column(
        SmallInteger,
        Computed("CASE WHEN status = 'DRAFT' THEN 1 END", persisted=True),
        nullable=True,
    )
    effective_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    priority: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=100)
    applies_branch_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    applies_department_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    applies_designation_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    applies_grade_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    applies_employment_type_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    applies_employee_category_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    applies_gender: Mapped[str | None] = mapped_column(String(50), nullable=True)
    applicability_specificity: Mapped[int] = mapped_column(
        SmallInteger, Computed(_SPECIFICITY_SQL, persisted=True), nullable=False
    )
    allocation_method: Mapped[str] = mapped_column(String(20), nullable=False, default="NONE")
    annual_entitlement_days: Mapped[Decimal | None] = _days(nullable=True)
    accrual_frequency: Mapped[str | None] = mapped_column(String(12), nullable=True)
    accrual_timing: Mapped[str | None] = mapped_column(String(12), nullable=True)
    joining_cutoff_day: Mapped[int | None] = mapped_column(SmallInteger, nullable=True, default=15)
    upfront_proration: Mapped[str | None] = mapped_column(String(10), nullable=True)
    rounding_mode: Mapped[str] = mapped_column(String(20), nullable=False, default="TWO_DECIMALS")
    max_balance_days: Mapped[Decimal | None] = _days(nullable=True)
    accrue_during_inactive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    carry_forward_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    max_carry_forward_days: Mapped[Decimal | None] = _days(nullable=True)
    negative_balance_year_end: Mapped[str] = mapped_column(
        String(15), nullable=False, default="CARRY_FORWARD"
    )
    allow_negative_balance: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    max_negative_balance_days: Mapped[Decimal | None] = _days(nullable=True)
    reserve_pending_applications: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    allow_half_day: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    min_days_per_application: Mapped[Decimal | None] = _days(nullable=True)
    max_days_per_application: Mapped[Decimal | None] = _days(nullable=True)
    max_consecutive_days: Mapped[Decimal | None] = _days(nullable=True)
    min_service_days: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    allowed_during_probation: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    allowed_during_notice: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    min_notice_days: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    max_backdate_days: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=30)
    max_future_days: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=365)
    count_weekly_offs: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    count_holidays: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    document_required_above_days: Mapped[Decimal | None] = _days(nullable=True)
    max_applications_per_year: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    approval_chain: Mapped[str] = mapped_column(String(20), nullable=False, default="MANAGER_ONLY")
    employee_cancellation_allowed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    change_note: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    published_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid7, ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    published_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)


class BackgroundJobRun(Base, StandardColumns):
    """Job run history and run-level idempotency (§14.2)."""

    __tablename__ = "background_job_runs"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_background_job_runs_org_id"),
        UniqueConstraint(
            "organization_id",
            "job_code",
            "run_key",
            "attempt_no",
            name="uq_background_job_runs_attempt",
        ),
        # One RUNNING row per (organization, job): the DB form of JOB_ALREADY_RUNNING.
        UniqueConstraint(
            "organization_id", "job_code", "running_key", name="uq_background_job_runs_one_running"
        ),
        CheckConstraint(_in("job_code", JobCode), name="ck_background_job_runs_code"),
        CheckConstraint(
            "`trigger` IN ('SCHEDULED','MANUAL','EVENT')", name="ck_background_job_runs_trigger"
        ),
        CheckConstraint(
            "status IN ('RUNNING','SUCCEEDED','PARTIALLY_FAILED','FAILED')",
            name="ck_background_job_runs_status",
        ),
        CheckConstraint(
            "processed_count >= 0 AND succeeded_count >= 0 AND skipped_count >= 0 "
            "AND failed_count >= 0",
            name="ck_background_job_runs_counts",
        ),
        tenant_index(
            "background_job_runs", "job_code", "started_at", name="ix_background_job_runs_started"
        ),
    )

    organization_id: Mapped[uuid.UUID] = _organization_column()
    job_code: Mapped[str] = mapped_column(String(30), nullable=False)
    run_key: Mapped[str] = mapped_column(String(100), nullable=False)
    attempt_no: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1)
    trigger: Mapped[str] = mapped_column("trigger", String(10), nullable=False)
    parameters: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="RUNNING")
    running_key: Mapped[int | None] = mapped_column(
        SmallInteger,
        Computed("CASE WHEN status = 'RUNNING' THEN 1 END", persisted=True),
        nullable=True,
    )
    started_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    heartbeat_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    processed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    succeeded_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    skipped_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failure_details: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    requested_by: Mapped[uuid.UUID] = mapped_column(
        Uuid7, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )


class LeaveBalance(Base, StandardColumns):
    """Materialized ledger sums and reservations (§6.5). Never archived or deleted."""

    __tablename__ = "leave_balances"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_leave_balances_org_id"),
        UniqueConstraint(
            "organization_id",
            "employee_id",
            "leave_type_id",
            "leave_year_start",
            name="uq_leave_balances_key",
        ),
        _org_fk("employees", "employee_id", "fk_leave_balances_employee"),
        _org_fk("leave_types", "leave_type_id", "fk_leave_balances_type"),
        CheckConstraint(
            "reserved_days >= 0 AND credited_days >= 0 AND debited_days >= 0",
            name="ck_leave_balances_non_negative",
        ),
        CheckConstraint("closure_status IN ('OPEN','CLOSED')", name="ck_leave_balances_closure"),
        CheckConstraint("leave_year_end > leave_year_start", name="ck_leave_balances_year"),
        tenant_index(
            "leave_balances",
            "leave_year_start",
            "leave_type_id",
            name="ix_leave_balances_year_type",
        ),
    )

    organization_id: Mapped[uuid.UUID] = _organization_column()
    employee_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    leave_type_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    leave_year_start: Mapped[date] = mapped_column(Date, nullable=False)
    leave_year_end: Mapped[date] = mapped_column(Date, nullable=False)
    credited_days: Mapped[Decimal] = _days(precision=9)
    debited_days: Mapped[Decimal] = _days(precision=9)
    posted_balance_days: Mapped[Decimal] = mapped_column(
        Numeric(9, 2), Computed("credited_days - debited_days", persisted=True), nullable=False
    )
    reserved_days: Mapped[Decimal] = _days(precision=9)
    available_days: Mapped[Decimal] = mapped_column(
        Numeric(9, 2),
        Computed("credited_days - debited_days - reserved_days", persisted=True),
        nullable=False,
    )
    closure_status: Mapped[str] = mapped_column(String(6), nullable=False, default="OPEN")
    last_transaction_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)


class LeaveApplication(Base, StandardColumns):
    """A leave request (§6.8). Never archived or deleted."""

    __tablename__ = "leave_applications"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_leave_applications_org_id"),
        UniqueConstraint("employee_id", "id", name="uq_leave_applications_employee_id"),
        _org_fk("employees", "employee_id", "fk_leave_applications_employee"),
        _org_fk("leave_types", "leave_type_id", "fk_leave_applications_type"),
        _org_fk("leave_policy_versions", "policy_version_id", "fk_leave_applications_version"),
        _org_fk("holidays", "holiday_id", "fk_leave_applications_holiday"),
        _org_fk("employee_documents", "supporting_document_id", "fk_leave_applications_document"),
        CheckConstraint(_in("status", ApplicationStatus), name="ck_leave_applications_status"),
        CheckConstraint(
            "end_date > start_date OR (end_date = start_date AND start_half <= end_half)",
            name="ck_leave_applications_range",
        ),
        CheckConstraint(
            "start_half IN ('FIRST_HALF','SECOND_HALF') AND "
            "end_half IN ('FIRST_HALF','SECOND_HALF')",
            name="ck_leave_applications_halves",
        ),
        CheckConstraint(
            "requested_days > 0 AND " + _HALF_STEP.format(column="requested_days"),
            name="ck_leave_applications_days",
        ),
        CheckConstraint(
            "status NOT IN ('APPROVED','CANCELLED') OR "
            "consumed_days + cancelled_days = requested_days",
            name="ck_leave_applications_consumed",
        ),
        CheckConstraint(
            "current_approver_type IS NULL OR current_approver_type IN ('MANAGER','HR_QUEUE')",
            name="ck_leave_applications_approver_type",
        ),
        tenant_index(
            "leave_applications", "employee_id", "start_date", name="ix_leave_applications_employee"
        ),
        tenant_index(
            "leave_applications",
            "status",
            "current_approver_user_id",
            name="ix_leave_applications_approver",
        ),
        tenant_index(
            "leave_applications",
            "leave_year_start",
            "leave_type_id",
            "status",
            name="ix_leave_applications_year_type",
        ),
    )

    organization_id: Mapped[uuid.UUID] = _organization_column()
    employee_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    leave_type_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    leave_type_code_snapshot: Mapped[str] = mapped_column(String(30), nullable=False)
    policy_version_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    leave_year_start: Mapped[date] = mapped_column(Date, nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    start_half: Mapped[str] = mapped_column(String(11), nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_half: Mapped[str] = mapped_column(String(11), nullable=False)
    holiday_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    requested_days: Mapped[Decimal] = _days()
    reason: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    supporting_document_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="SUBMITTED")
    reserves_balance: Mapped[bool] = mapped_column(Boolean, nullable=False)
    approval_chain_snapshot: Mapped[str] = mapped_column(String(20), nullable=False)
    current_approval_level: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    current_approver_type: Mapped[str | None] = mapped_column(String(10), nullable=True)
    current_approver_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid7, ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    current_approver_employee_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    submitted_by: Mapped[uuid.UUID] = mapped_column(
        Uuid7, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    submitted_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    submitted_on_behalf: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    on_behalf_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    decided_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid7, ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    decided_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    decision_comment: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    consumed_days: Mapped[Decimal] = _days()
    cancelled_days: Mapped[Decimal] = _days()
    has_partial_cancellation: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    cancellation_reason: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    schedule_review_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    resubmission_of_application_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)


class LeaveApplicationAction(Base, AppendOnlyColumns):
    """Append-only approval and cancellation history (§6.8, INT-39)."""

    __tablename__ = "leave_application_actions"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_leave_application_actions_org_id"),
        _org_fk("leave_applications", "application_id", "fk_leave_application_actions_app"),
        CheckConstraint(
            _in("action", ApplicationAction), name="ck_leave_application_actions_action"
        ),
        CheckConstraint(
            "restoration_mode IS NULL OR restoration_mode IN "
            "('NORMAL','RESTORE_TO_CURRENT_YEAR','NO_RESTORATION')",
            name="ck_leave_application_actions_mode",
        ),
        tenant_index(
            "leave_application_actions",
            "application_id",
            "occurred_at",
            name="ix_leave_application_actions_app",
        ),
    )

    organization_id: Mapped[uuid.UUID] = _organization_column()
    application_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    action: Mapped[str] = mapped_column(String(25), nullable=False)
    approval_level: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    from_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    to_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    from_approver_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    to_approver_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    from_approver_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    to_approver_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    requested_days_after: Mapped[Decimal] = _days()
    days_affected: Mapped[Decimal | None] = _days(nullable=True)
    cancel_from_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    cancel_from_half: Mapped[str | None] = mapped_column(String(11), nullable=True)
    restoration_mode: Mapped[str | None] = mapped_column(String(25), nullable=True)
    comment: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    actor_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid7, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    actor_employee_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)


class LeaveApplicationSlot(Base, StandardColumns):
    """One half-day covered by an application (§6.8)."""

    __tablename__ = "leave_application_slots"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_leave_application_slots_org_id"),
        # Partial unique (employee, date, half) WHERE ACTIVE: the overlap guarantee (LEV-26).
        UniqueConstraint(
            "employee_id",
            "slot_date",
            "half",
            "active_key",
            name="uq_leave_application_slots_active",
        ),
        _org_fk("employees", "employee_id", "fk_leave_application_slots_employee"),
        _org_fk("leave_applications", "application_id", "fk_leave_application_slots_app"),
        CheckConstraint(
            "half IN ('FIRST_HALF','SECOND_HALF')", name="ck_leave_application_slots_half"
        ),
        CheckConstraint(
            "day_type IN ('WORKING_DAY','WEEKLY_OFF','HOLIDAY')",
            name="ck_leave_application_slots_day",
        ),
        CheckConstraint(
            "slot_status IN ('ACTIVE','RELEASED','CANCELLED')",
            name="ck_leave_application_slots_status",
        ),
        tenant_index(
            "leave_application_slots", "application_id", name="ix_leave_application_slots_app"
        ),
        tenant_index(
            "leave_application_slots", "slot_date", name="ix_leave_application_slots_date"
        ),
    )

    organization_id: Mapped[uuid.UUID] = _organization_column()
    employee_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    application_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    slot_date: Mapped[date] = mapped_column(Date, nullable=False)
    half: Mapped[str] = mapped_column(String(11), nullable=False)
    day_type: Mapped[str] = mapped_column(String(12), nullable=False)
    counted: Mapped[bool] = mapped_column(Boolean, nullable=False)
    slot_status: Mapped[str] = mapped_column(String(10), nullable=False, default="ACTIVE")
    active_key: Mapped[int | None] = mapped_column(
        SmallInteger,
        Computed("CASE WHEN slot_status = 'ACTIVE' THEN 1 END", persisted=True),
        nullable=True,
    )
    cancelled_by_action_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)


class LeaveYearClosure(Base, StandardColumns):
    """Year-end outcome per (employee, type, year) (§6.7)."""

    __tablename__ = "leave_year_closures"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_leave_year_closures_org_id"),
        UniqueConstraint(
            "organization_id",
            "employee_id",
            "leave_type_id",
            "leave_year_start",
            name="uq_leave_year_closures_key",
        ),
        _org_fk("employees", "employee_id", "fk_leave_year_closures_employee"),
        _org_fk("leave_types", "leave_type_id", "fk_leave_year_closures_type"),
        CheckConstraint("status IN ('DEFERRED','CLOSED')", name="ck_leave_year_closures_status"),
        CheckConstraint(
            "status <> 'CLOSED' OR (closing_posted_balance IS NOT "
            "NULL AND carried_forward_days IS NOT NULL "
            "AND lapsed_days IS NOT NULL AND written_off_days IS NOT NULL)",
            name="ck_leave_year_closures_snapshot",
        ),
        tenant_index("leave_year_closures", "status", name="ix_leave_year_closures_status"),
    )

    organization_id: Mapped[uuid.UUID] = _organization_column()
    employee_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    leave_type_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    leave_year_start: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(8), nullable=False)
    deferred_reason: Mapped[str | None] = mapped_column(String(25), nullable=True)
    closing_posted_balance: Mapped[Decimal | None] = _days(nullable=True, precision=9)
    carried_forward_days: Mapped[Decimal | None] = _days(nullable=True, precision=9)
    lapsed_days: Mapped[Decimal | None] = _days(nullable=True, precision=9)
    written_off_days: Mapped[Decimal | None] = _days(nullable=True, precision=9)
    policy_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    processed_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    job_run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)


class LeaveTransaction(Base, AppendOnlyColumns):
    """The authoritative, append-only balance ledger (§6.5)."""

    __tablename__ = "leave_transactions"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_leave_transactions_org_id"),
        UniqueConstraint(
            "organization_id", "idempotency_key", name="uq_leave_transactions_idempotency"
        ),
        # One reversal per target; NULLs never collide.
        UniqueConstraint("reverses_transaction_id", name="uq_leave_transactions_one_reversal"),
        _org_fk("employees", "employee_id", "fk_leave_transactions_employee"),
        _org_fk("leave_types", "leave_type_id", "fk_leave_transactions_type"),
        _org_fk("leave_applications", "leave_application_id", "fk_leave_transactions_app"),
        _org_fk("leave_year_closures", "year_closure_id", "fk_leave_transactions_closure"),
        ForeignKeyConstraint(
            ["reverses_transaction_id"],
            ["leave_transactions.id"],
            name="fk_leave_transactions_reverses",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            _in("transaction_type", TransactionType), name="ck_leave_transactions_type"
        ),
        CheckConstraint(
            "(transaction_type IN ('ACCRUAL','ALLOCATION') AND quantity_days >= 0 "
            "AND entitled_quantity_days IS NOT NULL AND "
            "entitled_quantity_days >= quantity_days) OR "
            "(transaction_type IN "
            "('CONSUMPTION','LAPSE','ADJUSTMENT_DEBIT') AND quantity_days < 0) OR "
            "(transaction_type IN ('CONSUMPTION_REVERSAL','NEGATIVE_BALANCE_WRITE_OFF',"
            "'ADJUSTMENT_CREDIT') AND quantity_days > 0) OR "
            "(transaction_type IN ('OPENING_BALANCE','CARRY_FORWARD_OUT','CARRY_FORWARD_IN',"
            "'REVERSAL') AND quantity_days <> 0)",
            name="ck_leave_transactions_sign",
        ),
        CheckConstraint(
            "effective_date >= leave_year_start", name="ck_leave_transactions_effective"
        ),
        tenant_index(
            "leave_transactions",
            "employee_id",
            "leave_type_id",
            "leave_year_start",
            "effective_date",
            name="ix_leave_transactions_ledger",
        ),
        tenant_index(
            "leave_transactions", "leave_application_id", name="ix_leave_transactions_app"
        ),
    )

    organization_id: Mapped[uuid.UUID] = _organization_column()
    employee_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    leave_type_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    leave_year_start: Mapped[date] = mapped_column(Date, nullable=False)
    transaction_type: Mapped[str] = mapped_column(String(30), nullable=False)
    quantity_days: Mapped[Decimal] = _days()
    entitled_quantity_days: Mapped[Decimal | None] = _days(nullable=True)
    effective_date: Mapped[date] = mapped_column(Date, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    leave_application_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    application_action_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid7, ForeignKey("leave_application_actions.id", ondelete="RESTRICT"), nullable=True
    )
    reverses_transaction_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    year_closure_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    policy_version_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid7, ForeignKey("leave_policy_versions.id", ondelete="RESTRICT"), nullable=True
    )
    accrual_period_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    job_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid7, ForeignKey("background_job_runs.id", ondelete="RESTRICT"), nullable=True
    )
    reason: Mapped[str | None] = mapped_column(String(1000), nullable=True)


__all__ = [
    "ApplicationAction",
    "ApplicationStatus",
    "BackgroundJobRun",
    "JobCode",
    "LeaveApplication",
    "LeaveApplicationAction",
    "LeaveApplicationSlot",
    "LeaveBalance",
    "LeaveCategory",
    "LeavePolicy",
    "LeavePolicyVersion",
    "LeaveTransaction",
    "LeaveType",
    "LeaveYearClosure",
    "PolicyVersionStatus",
    "TransactionType",
]
