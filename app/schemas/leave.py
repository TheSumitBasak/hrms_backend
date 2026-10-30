"""Request and response contracts for leave management (addendum §6, §11)."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import Field, model_validator

from app.core.errors import WarningDetail
from app.schemas.common import ApiModel

CODE_PATTERN = r"^[A-Z0-9][A-Z0-9_./-]{0,29}$"
Days = Annotated[Decimal, Field(max_digits=7, decimal_places=2)]
HalfValue = Literal["FIRST_HALF", "SECOND_HALF"]
Reason = Annotated[str, Field(min_length=10, max_length=1000)]


# --------------------------------------------------------------------------- leave types


class LeaveTypeFields(ApiModel):
    leave_type_name: Annotated[str, Field(min_length=2, max_length=100)]
    description: Annotated[str, Field(max_length=500)] | None = None
    leave_category: Literal["STANDARD", "OPTIONAL_HOLIDAY"] = "STANDARD"
    paid_classification: Literal["PAID", "UNPAID", "NOT_SPECIFIED"] = "NOT_SPECIFIED"
    tracks_balance: bool = True
    show_type_in_team_calendar: bool = False
    encashment_eligible: bool = False
    display_order: Annotated[int, Field(ge=0)] = 0


class LeaveTypeCreateRequest(LeaveTypeFields):
    leave_type_code: Annotated[str, Field(pattern=CODE_PATTERN)]


class LeaveTypeUpdateRequest(ApiModel):
    leave_type_name: Annotated[str, Field(min_length=2, max_length=100)] | None = None
    description: Annotated[str, Field(max_length=500)] | None = None
    leave_category: Literal["STANDARD", "OPTIONAL_HOLIDAY"] | None = None
    paid_classification: Literal["PAID", "UNPAID", "NOT_SPECIFIED"] | None = None
    tracks_balance: bool | None = None
    show_type_in_team_calendar: bool | None = None
    encashment_eligible: bool | None = None
    display_order: Annotated[int, Field(ge=0)] | None = None


class StatusChangeRequest(ApiModel):
    reason: Annotated[str, Field(min_length=10, max_length=500)]


class LeaveTypeResponse(LeaveTypeFields):
    id: uuid.UUID
    organization_id: uuid.UUID
    leave_type_code: str
    status: str
    row_version: int = 0


# --------------------------------------------------------------------------- policies


class PolicyVersionFields(ApiModel):
    """Every editable column of ``leave_policy_versions`` (§6.3); all optional in a DRAFT."""

    effective_from: date | None = None
    priority: Annotated[int, Field(ge=1, le=999)] | None = None
    applies_branch_id: uuid.UUID | None = None
    applies_department_id: uuid.UUID | None = None
    applies_designation_id: uuid.UUID | None = None
    applies_grade_id: uuid.UUID | None = None
    applies_employment_type_id: uuid.UUID | None = None
    applies_employee_category_id: uuid.UUID | None = None
    applies_gender: Annotated[str, Field(max_length=50)] | None = None
    allocation_method: Literal["NONE", "ANNUAL_UPFRONT", "PERIODIC_ACCRUAL"] | None = None
    annual_entitlement_days: Annotated[Decimal, Field(gt=0, le=366)] | None = None
    accrual_frequency: Literal["MONTHLY", "QUARTERLY", "HALF_YEARLY"] | None = None
    accrual_timing: Literal["PERIOD_START", "PERIOD_END"] | None = None
    joining_cutoff_day: Annotated[int, Field(ge=1, le=31)] | None = None
    upfront_proration: Literal["NONE", "MONTHLY"] | None = None
    rounding_mode: Literal["TWO_DECIMALS", "NEAREST_HALF_DAY", "DOWN_TO_HALF_DAY"] | None = None
    max_balance_days: Annotated[Decimal, Field(gt=0)] | None = None
    accrue_during_inactive: bool | None = None
    carry_forward_enabled: bool | None = None
    max_carry_forward_days: Annotated[Decimal, Field(ge=0)] | None = None
    negative_balance_year_end: Literal["CARRY_FORWARD", "WRITE_OFF"] | None = None
    allow_negative_balance: bool | None = None
    max_negative_balance_days: Annotated[Decimal, Field(gt=0)] | None = None
    reserve_pending_applications: bool | None = None
    allow_half_day: bool | None = None
    min_days_per_application: Annotated[Decimal, Field(gt=0)] | None = None
    max_days_per_application: Annotated[Decimal, Field(gt=0)] | None = None
    max_consecutive_days: Annotated[Decimal, Field(gt=0)] | None = None
    min_service_days: Annotated[int, Field(ge=0, le=3650)] | None = None
    allowed_during_probation: bool | None = None
    allowed_during_notice: bool | None = None
    min_notice_days: Annotated[int, Field(ge=0, le=365)] | None = None
    max_backdate_days: Annotated[int, Field(ge=0, le=365)] | None = None
    max_future_days: Annotated[int, Field(ge=0, le=730)] | None = None
    count_weekly_offs: bool | None = None
    count_holidays: bool | None = None
    document_required_above_days: Annotated[Decimal, Field(ge=0)] | None = None
    max_applications_per_year: Annotated[int, Field(ge=1, le=366)] | None = None
    approval_chain: Literal["MANAGER_ONLY", "HR_ONLY", "MANAGER_THEN_HR"] | None = None
    employee_cancellation_allowed: bool | None = None
    change_note: Annotated[str, Field(max_length=1000)] | None = None


class PolicyVersionResponse(PolicyVersionFields):
    id: uuid.UUID
    policy_id: uuid.UUID
    leave_type_id: uuid.UUID
    version_number: int
    status: str
    effective_to: date | None = None
    applicability_specificity: int | None = None
    published_at: datetime | None = None
    row_version: int = 0


class PolicyCreateRequest(ApiModel):
    leave_type_id: uuid.UUID
    policy_code: Annotated[str, Field(pattern=CODE_PATTERN)]
    policy_name: Annotated[str, Field(min_length=2, max_length=150)]
    description: Annotated[str, Field(max_length=1000)] | None = None
    version: PolicyVersionFields = Field(default_factory=PolicyVersionFields)


class PolicyUpdateRequest(ApiModel):
    policy_name: Annotated[str, Field(min_length=2, max_length=150)] | None = None
    description: Annotated[str, Field(max_length=1000)] | None = None
    status: Literal["ACTIVE", "INACTIVE"] | None = None


class PolicyResponse(ApiModel):
    id: uuid.UUID
    organization_id: uuid.UUID
    leave_type_id: uuid.UUID
    policy_code: str
    policy_name: str
    description: str | None = None
    status: str
    versions: list[PolicyVersionResponse] = Field(default_factory=list)
    row_version: int = 0


class PolicyResolveRequest(ApiModel):
    employee_id: uuid.UUID
    leave_type_id: uuid.UUID
    on: date = Field(alias="date")


class ArchiveVersionRequest(ApiModel):
    reason: Annotated[str, Field(min_length=10, max_length=500)]


# --------------------------------------------------------------------------- ledger


class AdminTransactionRequest(ApiModel):
    transaction_type: Literal["OPENING_BALANCE", "ADJUSTMENT_CREDIT", "ADJUSTMENT_DEBIT"]
    leave_type_id: uuid.UUID
    leave_year_start: date
    quantity_days: Days
    effective_date: date
    reason: Annotated[str, Field(min_length=20, max_length=1000)]
    acknowledged_warnings: list[str] = Field(default_factory=list)


class ReverseTransactionRequest(ApiModel):
    reason: Annotated[str, Field(min_length=20, max_length=1000)]


class TransactionResponse(ApiModel):
    id: uuid.UUID
    employee_id: uuid.UUID
    leave_type_id: uuid.UUID
    leave_year_start: date
    transaction_type: str
    quantity_days: Decimal
    entitled_quantity_days: Decimal | None = None
    effective_date: date
    leave_application_id: uuid.UUID | None = None
    reverses_transaction_id: uuid.UUID | None = None
    created_at: datetime
    reason: str | None = None  # omitted for SELF (§10.5)


class BalanceResponse(ApiModel):
    leave_type_id: uuid.UUID
    leave_type_code: str
    leave_type_name: str
    leave_year_start: date
    leave_year_end: date
    credited_days: Decimal
    debited_days: Decimal
    posted_balance_days: Decimal
    reserved_days: Decimal
    available_days: Decimal
    closure_status: str
    posted_balance_as_of: Decimal | None = None


class BalancesResponse(ApiModel):
    employee_id: uuid.UUID
    leave_year_start: date
    items: list[BalanceResponse]


class TransactionWithBalance(ApiModel):
    transaction: TransactionResponse
    balance: BalanceResponse | None = None


# --------------------------------------------------------------------------- applications


class ApplicationRequest(ApiModel):
    leave_type_id: uuid.UUID
    start_date: date
    start_half: HalfValue = "FIRST_HALF"
    end_date: date
    end_half: HalfValue = "SECOND_HALF"
    holiday_id: uuid.UUID | None = None
    reason: Annotated[str, Field(max_length=1000)] | None = None
    supporting_document_id: uuid.UUID | None = None
    resubmission_of_application_id: uuid.UUID | None = None
    acknowledged_warnings: list[str] = Field(default_factory=list)


class OnBehalfApplicationRequest(ApplicationRequest):
    on_behalf_reason: Annotated[str, Field(min_length=10, max_length=500)]


class PreviewRequest(ApplicationRequest):
    employee_id: uuid.UUID | None = None


class SlotResponse(ApiModel):
    slot_date: date
    half: str
    day_type: str
    counted: bool
    slot_status: str | None = None


class ActionResponse(ApiModel):
    id: uuid.UUID
    action: str
    approval_level: int | None = None
    from_status: str | None = None
    to_status: str | None = None
    to_approver_type: str | None = None
    requested_days_after: Decimal
    days_affected: Decimal | None = None
    restoration_mode: str | None = None
    comment: str | None = None
    actor_user_id: uuid.UUID
    occurred_at: datetime


class ApplicationResponse(ApiModel):
    id: uuid.UUID
    employee_id: uuid.UUID
    leave_type_id: uuid.UUID
    leave_type_code_snapshot: str
    policy_version_id: uuid.UUID
    leave_year_start: date
    start_date: date
    start_half: str
    end_date: date
    end_half: str
    requested_days: Decimal
    consumed_days: Decimal
    cancelled_days: Decimal
    has_partial_cancellation: bool
    status: str
    reserves_balance: bool
    approval_chain_snapshot: str
    current_approval_level: int | None = None
    current_approver_type: str | None = None
    current_approver_user_id: uuid.UUID | None = None
    submitted_by: uuid.UUID
    submitted_at: datetime
    submitted_on_behalf: bool
    decided_at: datetime | None = None
    schedule_review_required: bool
    reason: str | None = None  # omitted when the caller may not see it (§10.5)
    decision_comment: str | None = None
    holiday_id: uuid.UUID | None = None
    supporting_document_attached: bool = False
    row_version: int = 0
    slots: list[SlotResponse] = Field(default_factory=list)
    actions: list[ActionResponse] = Field(default_factory=list)
    balance: BalanceResponse | None = None
    restored_days: Decimal | None = None


class PreviewResponse(ApiModel):
    leave_type_id: uuid.UUID
    leave_year_start: date
    requested_days: Decimal
    slots: list[SlotResponse]
    available_before: Decimal | None = None
    available_after: Decimal | None = None
    policy_version_id: uuid.UUID
    warnings: list[WarningDetail] = Field(default_factory=list)


class ApproveRequest(ApiModel):
    comment: Annotated[str, Field(max_length=1000)] | None = None
    acknowledged_warnings: list[str] = Field(default_factory=list)


class RejectRequest(ApiModel):
    decision_comment: Annotated[str, Field(min_length=10, max_length=1000)]


class WithdrawRequest(ApiModel):
    reason: Annotated[str, Field(max_length=1000)] | None = None


class CancelRequest(ApiModel):
    scope: Literal["FULL", "FROM"] = "FULL"
    cancel_from_date: date | None = None
    cancel_from_half: HalfValue | None = None
    reason: Annotated[str, Field(min_length=10, max_length=1000)]
    restoration_mode: Literal["RESTORE_TO_CURRENT_YEAR", "NO_RESTORATION"] | None = None

    @model_validator(mode="after")
    def _from_needs_origin(self) -> CancelRequest:
        if self.scope == "FROM" and (
            self.cancel_from_date is None or self.cancel_from_half is None
        ):
            raise ValueError("cancel_from_date and cancel_from_half are required for scope FROM")
        return self


class ReassignRequest(ApiModel):
    to_user_id: uuid.UUID | None = None
    to_hr_queue: bool = False
    reason: Annotated[str, Field(min_length=10, max_length=1000)]


class RecalculateRequest(ApiModel):
    reason: Annotated[str, Field(min_length=20, max_length=1000)]


class JobRunRequest(ApiModel):
    job_code: Literal["LEAVE_ACCRUAL", "LEAVE_YEAR_END"]
    as_of_date: date


class JobRunResponse(ApiModel):
    id: uuid.UUID
    job_code: str
    run_key: str
    attempt_no: int
    trigger: str
    status: str
    started_at: datetime
    finished_at: datetime | None = None
    processed_count: int
    succeeded_count: int
    skipped_count: int
    failed_count: int
    failure_details: list[dict[str, str]] | None = None
