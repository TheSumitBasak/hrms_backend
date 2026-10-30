"""Request and response contracts for attendance (addendum 5.3-5.16, 11)."""

from __future__ import annotations

import uuid
from datetime import date, datetime, time
from typing import Annotated, Any, Literal

from pydantic import Field, model_validator

from app.core.errors import WarningDetail
from app.schemas.common import ApiModel

CODE_PATTERN = r"^[A-Z0-9][A-Z0-9_./-]{0,29}$"
HalfValue = Literal["FIRST_HALF", "SECOND_HALF"]
PunchType = Literal["IN", "OUT"]
Arrangement = Literal["OFFICE", "REMOTE"]


# Shifts.


class ShiftFields(ApiModel):
    shift_name: Annotated[str, Field(min_length=2, max_length=100)]
    start_time: time
    end_time: time
    break_minutes: Annotated[int, Field(ge=0)] = 0
    full_day_min_work_minutes: Annotated[int, Field(ge=1)]
    half_day_min_work_minutes: Annotated[int, Field(ge=1)]
    window_before_minutes: Annotated[int, Field(ge=0, le=720)] = 120
    window_after_minutes: Annotated[int, Field(ge=0, le=720)] = 240


class ShiftCreateRequest(ShiftFields):
    shift_code: Annotated[str, Field(pattern=CODE_PATTERN)]


class ShiftUpdateRequest(ApiModel):
    shift_name: Annotated[str, Field(min_length=2, max_length=100)] | None = None
    start_time: time | None = None
    end_time: time | None = None
    break_minutes: Annotated[int, Field(ge=0)] | None = None
    full_day_min_work_minutes: Annotated[int, Field(ge=1)] | None = None
    half_day_min_work_minutes: Annotated[int, Field(ge=1)] | None = None
    window_before_minutes: Annotated[int, Field(ge=0, le=720)] | None = None
    window_after_minutes: Annotated[int, Field(ge=0, le=720)] | None = None
    status: Literal["ACTIVE", "INACTIVE"] | None = None


class ShiftResponse(ShiftFields):
    id: uuid.UUID
    shift_code: str
    is_overnight: bool
    duration_minutes: int
    status: str
    row_version: int = 0


# Policies.


class PolicyVersionFields(ApiModel):
    effective_from: date | None = None
    priority: Annotated[int, Field(ge=1, le=999)] | None = None
    applies_branch_id: uuid.UUID | None = None
    applies_department_id: uuid.UUID | None = None
    applies_work_location_id: uuid.UUID | None = None
    applies_employment_type_id: uuid.UUID | None = None
    applies_employee_category_id: uuid.UUID | None = None
    timezone: Annotated[str, Field(max_length=50)] | None = None
    default_shift_id: uuid.UUID | None = None
    default_weekly_off_pattern_id: uuid.UUID | None = None
    punch_evaluation_mode: Literal["FIRST_IN_LAST_OUT", "PAIRED_PUNCHES"] | None = None
    break_deduction_mode: Literal["NONE", "FIXED_FROM_SHIFT"] | None = None
    late_grace_minutes: Annotated[int, Field(ge=0, le=240)] | None = None
    early_departure_grace_minutes: Annotated[int, Field(ge=0, le=240)] | None = None
    flexible_full_day_min_work_minutes: Annotated[int, Field(ge=1, le=1440)] | None = None
    flexible_half_day_min_work_minutes: Annotated[int, Field(ge=1, le=1440)] | None = None
    flexible_day_start_time: time | None = None
    self_punch_enabled: bool | None = None
    allow_remote_self_punch: bool | None = None
    manual_punch_enabled: bool | None = None
    integration_punch_enabled: bool | None = None
    duplicate_punch_window_seconds: Annotated[int, Field(ge=0, le=600)] | None = None
    regularization_enabled: bool | None = None
    regularization_backdate_limit_days: Annotated[int, Field(ge=0, le=90)] | None = None
    on_duty_requests_enabled: bool | None = None
    regularization_approval_chain: Literal["MANAGER_ONLY", "HR_ONLY", "MANAGER_THEN_HR"] | None = (
        None
    )
    auto_finalize_after_days: Annotated[int, Field(ge=1, le=90)] | None = None
    incomplete_on_finalize: Literal["BLOCK", "MARK_ABSENT"] | None = None
    change_note: Annotated[str, Field(max_length=1000)] | None = None


class PolicyVersionResponse(PolicyVersionFields):
    id: uuid.UUID
    policy_id: uuid.UUID
    version_number: int
    status: str
    effective_to: date | None = None
    applicability_specificity: int | None = None
    published_at: datetime | None = None
    row_version: int = 0


class PolicyCreateRequest(ApiModel):
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
    policy_code: str
    policy_name: str
    description: str | None = None
    status: str
    versions: list[PolicyVersionResponse] = Field(default_factory=list)
    row_version: int = 0


class ArchiveRequest(ApiModel):
    reason: Annotated[str, Field(min_length=10, max_length=500)]


class ResolveRequest(ApiModel):
    employee_id: uuid.UUID
    on: date = Field(alias="date")


class ScheduleResponse(ApiModel):
    day_type: str
    holiday_id: uuid.UUID | None = None
    holiday_calendar_id: uuid.UUID | None = None
    weekly_off_pattern_id: uuid.UUID | None = None
    policy_version_id: uuid.UUID | None = None
    shift_id: uuid.UUID | None = None
    shift_assignment_id: uuid.UUID | None = None
    flexible: bool


# Shift assignments.


class AssignmentRequest(ApiModel):
    shift_id: uuid.UUID | None = None
    weekly_off_pattern_id: uuid.UUID | None = None
    effective_from: date
    effective_to: date | None = None
    reason: Annotated[str, Field(max_length=500)] | None = None
    acknowledged_warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _has_target(self) -> AssignmentRequest:
        if self.shift_id is None and self.weekly_off_pattern_id is None:
            raise ValueError("shift_id or weekly_off_pattern_id is required")
        return self


class AssignmentResponse(ApiModel):
    id: uuid.UUID
    employee_id: uuid.UUID
    shift_id: uuid.UUID | None = None
    weekly_off_pattern_id: uuid.UUID | None = None
    effective_from: date
    effective_to: date | None = None
    reason: str | None = None
    boundary_adjustment: dict[str, Any] | None = None
    row_version: int = 0


# Punches and records.


class SelfPunchRequest(ApiModel):
    punch_type: PunchType
    work_arrangement: Arrangement | None = None


class ManualPunchRequest(ApiModel):
    punch_at: datetime
    punch_type: PunchType
    work_arrangement: Arrangement | None = None
    remarks: Annotated[str, Field(min_length=10, max_length=500)]


class ImportItem(ApiModel):
    employee_id: uuid.UUID | None = None
    employee_code: Annotated[str, Field(max_length=30)] | None = None
    punch_at: datetime
    punch_type: PunchType
    integration_source_code: Annotated[str, Field(max_length=30)]
    external_event_id: Annotated[str, Field(max_length=100)]

    @model_validator(mode="after")
    def _has_employee(self) -> ImportItem:
        if (self.employee_id is None) == (self.employee_code is None):
            raise ValueError("provide exactly one of employee_id or employee_code")
        return self


class ImportRequest(ApiModel):
    items: Annotated[list[ImportItem], Field(min_length=1, max_length=1000)]


class ImportOutcome(ApiModel):
    external_event_id: str
    outcome: str
    error_code: str | None = None
    punch_id: uuid.UUID | None = None


class ImportResponse(ApiModel):
    results: list[ImportOutcome]


class PunchResponse(ApiModel):
    id: uuid.UUID
    employee_id: uuid.UUID
    punch_at: datetime
    punch_type: str
    source: str
    work_arrangement: str | None = None
    business_date: date
    punch_status: str
    duplicate_of_punch_id: uuid.UUID | None = None
    void_reason: str | None = None


class RecordResponse(ApiModel):
    id: uuid.UUID
    employee_id: uuid.UUID
    business_date: date
    record_state: str
    day_type: str
    day_status: str
    first_half_status: str | None = None
    second_half_status: str | None = None
    first_half_source: str
    second_half_source: str
    first_half_leave_application_id: uuid.UUID | None = None
    second_half_leave_application_id: uuid.UUID | None = None
    shift_code_snapshot: str | None = None
    scheduled_start_at: datetime | None = None
    scheduled_end_at: datetime | None = None
    first_in_at: datetime | None = None
    last_out_at: datetime | None = None
    gross_minutes: int
    break_deducted_minutes: int
    worked_minutes: int | None = None
    late_by_minutes: int | None = None
    early_by_minutes: int | None = None
    is_late: bool
    is_early_departure: bool
    has_missing_punch: bool
    has_out_of_window_punch: bool
    has_held_punch: bool
    punch_on_leave_conflict: bool
    worked_on_non_working_day: bool
    work_arrangement: str
    has_pending_regularization: bool
    post_finalization_change: bool
    revision_count: int = 0
    remarks: str | None = None  # attendance.correct holders only
    row_version: int = 0


class PunchResult(ApiModel):
    punch: PunchResponse
    outcome: str
    duplicate_of: uuid.UUID | None = None
    record: RecordResponse


class RecordDetail(RecordResponse):
    punches: list[PunchResponse] = Field(default_factory=list)


class FinalizeRequest(ApiModel):
    employee_id: uuid.UUID
    start: date = Field(alias="from")
    end: date = Field(alias="to")


class FinalizeSkip(ApiModel):
    record_id: uuid.UUID
    reason: str


class FinalizeResponse(ApiModel):
    finalized_count: int
    skipped: list[FinalizeSkip]


class ReopenRequest(ApiModel):
    reason: Annotated[str, Field(min_length=20, max_length=1000)]


class AddPunch(ApiModel):
    punch_at: datetime
    punch_type: PunchType
    work_arrangement: Arrangement | None = None


class HalfStatus(ApiModel):
    half: HalfValue
    status: Literal["PRESENT", "ABSENT"]


class HalfOnly(ApiModel):
    half: HalfValue


class CorrectionOperation(ApiModel):
    add_punch: AddPunch | None = None
    void_punch_id: uuid.UUID | None = None
    apply_held_punch_id: uuid.UUID | None = None
    set_override: HalfStatus | None = None
    clear_override: HalfOnly | None = None
    set_on_duty: HalfOnly | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> CorrectionOperation:
        if sum(v is not None for v in self.model_dump().values()) != 1:
            raise ValueError("each operation must set exactly one action")
        return self


class CorrectionRequest(ApiModel):
    operations: Annotated[list[CorrectionOperation], Field(min_length=1, max_length=50)]
    reason: Annotated[str, Field(min_length=20, max_length=1000)]


class RevisionResponse(ApiModel):
    revision_no: int
    change_source: str
    before_snapshot: dict[str, Any]
    after_snapshot: dict[str, Any]
    related_punch_ids: list[str] | None = None
    related_leave_application_id: uuid.UUID | None = None
    reason: str | None = None
    actor_user_id: uuid.UUID
    occurred_at: datetime


class WarningsEnvelope(ApiModel):
    warnings: list[WarningDetail] = Field(default_factory=list)


class DayCloseRequest(ApiModel):
    business_date: date


# Regularization (5.14).


class RegularizationItemIn(ApiModel):
    item_type: Literal["ADD_PUNCH", "VOID_PUNCH", "SET_ON_DUTY"]
    punch_type: PunchType | None = None
    punch_at: datetime | None = None
    work_arrangement: Arrangement | None = None
    target_punch_id: uuid.UUID | None = None
    half: HalfValue | None = None

    @model_validator(mode="after")
    def _fields_match_type(self) -> RegularizationItemIn:
        add = self.punch_type is not None and self.punch_at is not None
        if (self.item_type == "ADD_PUNCH") != add:
            raise ValueError("ADD_PUNCH needs punch_type and punch_at, and only it may carry them")
        if (self.item_type == "VOID_PUNCH") != (self.target_punch_id is not None):
            raise ValueError("VOID_PUNCH needs target_punch_id, and only it may carry one")
        if (self.item_type == "SET_ON_DUTY") != (self.half is not None):
            raise ValueError("SET_ON_DUTY needs half, and only it may carry one")
        return self


class RegularizationRequestBody(ApiModel):
    business_date: date
    request_type: Literal["MISSING_PUNCH", "PUNCH_CORRECTION", "ON_DUTY"]
    reason: Annotated[str, Field(min_length=10, max_length=1000)]
    items: Annotated[list[RegularizationItemIn], Field(min_length=1, max_length=10)]
    supporting_document_id: uuid.UUID | None = None
    resubmission_of_request_id: uuid.UUID | None = None


class RegularizationOnBehalfBody(RegularizationRequestBody):
    on_behalf_reason: Annotated[str, Field(min_length=10, max_length=500)]


class RegularizationApproveBody(ApiModel):
    comment: Annotated[str, Field(max_length=1000)] | None = None


class RegularizationRejectBody(ApiModel):
    decision_comment: Annotated[str, Field(min_length=10, max_length=1000)]


class RegularizationWithdrawBody(ApiModel):
    reason: Annotated[str, Field(max_length=500)] | None = None


class RegularizationReassignBody(ApiModel):
    to_user_id: uuid.UUID | None = None
    to_hr_queue: bool = False
    reason: Annotated[str, Field(min_length=10, max_length=1000)]


class RegularizationItemOut(ApiModel):
    id: uuid.UUID
    item_type: str
    punch_type: str | None = None
    punch_at: datetime | None = None
    work_arrangement: str | None = None
    target_punch_id: uuid.UUID | None = None
    half: str | None = None
    created_punch_id: uuid.UUID | None = None


class RegularizationActionOut(ApiModel):
    id: uuid.UUID
    action: str
    approval_level: int | None = None
    from_status: str | None = None
    to_status: str | None = None
    to_approver_type: str | None = None
    comment: str | None = None
    actor_user_id: uuid.UUID
    occurred_at: datetime


class RegularizationResponse(ApiModel):
    id: uuid.UUID
    employee_id: uuid.UUID
    attendance_record_id: uuid.UUID
    business_date: date
    request_type: str
    reason: str
    status: str
    approval_chain_snapshot: str
    current_approval_level: int | None = None
    current_approver_type: str | None = None
    current_approver_user_id: uuid.UUID | None = None
    submitted_by: uuid.UUID
    submitted_at: datetime
    submitted_on_behalf: bool
    decided_at: datetime | None = None
    decision_comment: str | None = None
    supporting_document_attached: bool = False
    row_version: int = 0
    items: list[RegularizationItemOut] = Field(default_factory=list)
    actions: list[RegularizationActionOut] = Field(default_factory=list)
