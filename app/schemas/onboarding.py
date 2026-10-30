"""Request and response contracts for onboarding (addendum 4.14, 11)."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Annotated, Any, Literal

from pydantic import Field

from app.core.errors import WarningDetail
from app.schemas.common import ApiModel

CODE_PATTERN = r"^[A-Z0-9][A-Z0-9_./-]{0,29}$"
Phase = Literal["PRE_JOINING", "JOINING_DAY", "POST_JOINING"]
TaskType = Literal["CHECKLIST", "DOCUMENT_UPLOAD", "DATA_SUBMISSION", "ACKNOWLEDGEMENT"]
OwnerType = Literal[
    "HR_COORDINATOR", "REPORTING_MANAGER", "EMPLOYEE", "SPECIFIC_USER", "DEPARTMENT_QUEUE"
]
Reason10 = Annotated[str, Field(min_length=10, max_length=1000)]


# Templates.


class TaskIn(ApiModel):
    """A template task, or an ad-hoc task on a running instance (4.3)."""

    task_code: Annotated[str, Field(pattern=CODE_PATTERN)]
    title: Annotated[str, Field(min_length=2, max_length=200)]
    instructions: Annotated[str, Field(max_length=2000)] | None = None
    phase: Phase
    task_type: TaskType
    owner_type: OwnerType
    owner_user_id: uuid.UUID | None = None
    owner_department_id: uuid.UUID | None = None
    is_mandatory: bool = True
    blocks_activation: bool = False
    is_waivable: bool | None = None
    review_required: bool = False
    reviewer_type: Literal["HR_COORDINATOR", "SPECIFIC_USER", "DEPARTMENT_QUEUE"] | None = None
    reviewer_user_id: uuid.UUID | None = None
    reviewer_department_id: uuid.UUID | None = None
    due_basis: Literal["JOINING_DATE", "INITIATION_DATE"] = "JOINING_DATE"
    due_offset_days: Annotated[int, Field(ge=-365, le=365)] = 0
    required_document_category_id: uuid.UUID | None = None
    requires_document_verification: bool | None = None
    min_documents: Annotated[int, Field(ge=1, le=20)] | None = None
    data_section_code: (
        Literal[
            "PERSONAL",
            "CONTACT",
            "IDENTIFIERS",
            "EDUCATION",
            "PREVIOUS_EMPLOYMENT",
            "FAMILY",
            "NOMINATIONS",
            "BANK",
        ]
        | None
    ) = None
    acknowledgement_kind: Literal["PRIVACY_NOTICE", "POLICY"] | None = None
    policy_file_id: uuid.UUID | None = None
    policy_version_label: Annotated[str, Field(max_length=20)] | None = None
    privacy_notice_basis: Literal["NOTICE_ONLY", "CONSENT"] | None = None
    consent_scope: Annotated[str, Field(max_length=200)] | None = None
    acknowledgement_statement: Annotated[str, Field(max_length=1000)] | None = None
    requires_evidence: bool = False
    visible_to_employee: bool = False
    display_order: Annotated[int, Field(ge=0)] = 0


class DependencyIn(ApiModel):
    task_code: str
    prerequisite_task_code: str


class VersionFields(ApiModel):
    effective_from: date | None = None
    priority: Annotated[int, Field(ge=1, le=999)] | None = None
    applies_branch_id: uuid.UUID | None = None
    applies_department_id: uuid.UUID | None = None
    applies_designation_id: uuid.UUID | None = None
    applies_grade_id: uuid.UUID | None = None
    applies_employment_type_id: uuid.UUID | None = None
    applies_employee_category_id: uuid.UUID | None = None
    change_note: Annotated[str, Field(max_length=1000)] | None = None


class TemplateCreateRequest(ApiModel):
    template_code: Annotated[str, Field(pattern=CODE_PATTERN)]
    template_name: Annotated[str, Field(min_length=2, max_length=150)]
    description: Annotated[str, Field(max_length=1000)] | None = None
    version: VersionFields = Field(default_factory=VersionFields)
    tasks: list[TaskIn] = Field(default_factory=list, max_length=200)
    dependencies: list[DependencyIn] = Field(default_factory=list, max_length=1000)


class TemplateUpdateRequest(ApiModel):
    template_name: Annotated[str, Field(min_length=2, max_length=150)] | None = None
    description: Annotated[str, Field(max_length=1000)] | None = None


class StatusReason(ApiModel):
    reason: Annotated[str, Field(min_length=10, max_length=500)]


class ReplaceTasksRequest(ApiModel):
    tasks: list[TaskIn] = Field(max_length=200)
    dependencies: list[DependencyIn] = Field(default_factory=list, max_length=1000)


class TemplateTaskOut(TaskIn):
    id: uuid.UUID


class VersionOut(VersionFields):
    id: uuid.UUID
    template_id: uuid.UUID
    version_number: int
    status: str
    effective_to: date | None = None
    applicability_specificity: int | None = None
    published_at: datetime | None = None
    row_version: int = 0
    tasks: list[TemplateTaskOut] = Field(default_factory=list)
    dependencies: list[DependencyIn] = Field(default_factory=list)


class TemplateOut(ApiModel):
    id: uuid.UUID
    template_code: str
    template_name: str
    description: str | None = None
    status: str
    row_version: int = 0
    versions: list[VersionOut] = Field(default_factory=list)


class ResolveRequest(ApiModel):
    employee_id: uuid.UUID
    basis_date: date | None = None


class CandidateOut(ApiModel):
    template_id: uuid.UUID
    template_code: str
    version_id: uuid.UUID
    version_number: int
    applicability_specificity: int
    priority: int
    effective_from: date


class ResolveResponse(ApiModel):
    selected: CandidateOut
    candidates: list[CandidateOut]


# Instances and tasks.


class InitiateRequest(ApiModel):
    template_id: uuid.UUID | None = None
    hr_coordinator_user_id: uuid.UUID | None = None
    target_completion_date: date | None = None
    remarks: Annotated[str, Field(max_length=2000)] | None = None
    acknowledged_warnings: list[str] = Field(default_factory=list)


class InstanceUpdateRequest(ApiModel):
    hr_coordinator_user_id: uuid.UUID | None = None
    target_completion_date: date | None = None
    remarks: Annotated[str, Field(max_length=2000)] | None = None


class ReasonBody(ApiModel):
    reason: Annotated[str, Field(min_length=10, max_length=1000)]


class ReopenInstanceBody(ApiModel):
    reason: Annotated[str, Field(min_length=20, max_length=1000)]


class TaskActionBody(ApiModel):
    completion_remarks: Annotated[str, Field(max_length=1000)] | None = None
    on_behalf_reason: Annotated[str, Field(max_length=500)] | None = None


class RejectBody(ApiModel):
    rejection_reason: Reason10


class WaiveBody(ApiModel):
    waiver_reason: Annotated[str, Field(min_length=20, max_length=1000)]


class TaskReopenBody(ApiModel):
    reason: Annotated[str, Field(min_length=10, max_length=1000)]
    acknowledged_warnings: list[str] = Field(default_factory=list)


class ReassignBody(ApiModel):
    assignee_user_id: uuid.UUID | None = None
    assignee_department_id: uuid.UUID | None = None
    reason: Annotated[str, Field(min_length=10, max_length=1000)]


class DueDateBody(ApiModel):
    due_date: date
    reason: Annotated[str, Field(min_length=10, max_length=1000)]


class AdHocTaskRequest(TaskIn):
    prerequisite_task_ids: list[uuid.UUID] = Field(default_factory=list, max_length=50)


class LinkBody(ApiModel):
    document_id: uuid.UUID


class UnlinkBody(ApiModel):
    reason: Annotated[str, Field(min_length=10, max_length=500)]


class PaperAckBody(ApiModel):
    supporting_document_id: uuid.UUID
    delivered_at: datetime | None = None
    on_behalf_reason: Annotated[str, Field(min_length=10, max_length=500)]


class TaskOut(ApiModel):
    id: uuid.UUID
    instance_id: uuid.UUID
    employee_id: uuid.UUID
    task_code: str
    title: str
    instructions: str | None = None
    phase: str
    task_type: str
    owner_type: str
    is_mandatory: bool
    blocks_activation: bool
    is_waivable: bool
    review_required: bool
    data_section_code: str | None = None
    acknowledgement_kind: str | None = None
    acknowledgement_statement: str | None = None
    policy_version_label: str | None = None
    required_document_category_code_snapshot: str | None = None
    min_documents: int | None = None
    requires_evidence: bool
    visible_to_employee: bool
    display_order: int
    assignee_user_id: uuid.UUID | None = None
    assignee_department_id: uuid.UUID | None = None
    assignee_resolution: str | None = None
    reviewer_user_id: uuid.UUID | None = None
    due_date: date | None = None
    due_date_source: str
    status: str
    is_blocked: bool = False
    is_overdue: bool = False
    rejection_reason: str | None = None
    waiver_reason: str | None = None
    completion_remarks: str | None = None
    acted_on_behalf: bool
    reopen_count: int
    row_version: int = 0


class TaskEventOut(ApiModel):
    id: uuid.UUID
    event_type: str
    from_status: str | None = None
    to_status: str | None = None
    source: str
    reason: str | None = None
    acted_on_behalf: bool
    actor_user_id: uuid.UUID
    occurred_at: datetime


class TaskDocumentOut(ApiModel):
    id: uuid.UUID
    document_id: uuid.UUID
    link_status: str
    linked_at: datetime


class TaskDetail(TaskOut):
    events: list[TaskEventOut] = Field(default_factory=list)
    documents: list[TaskDocumentOut] = Field(default_factory=list)


class InstanceEventOut(ApiModel):
    id: uuid.UUID
    event_type: str
    from_status: str | None = None
    to_status: str | None = None
    reason: str | None = None
    actor_user_id: uuid.UUID
    occurred_at: datetime


class InstanceOut(ApiModel):
    id: uuid.UUID
    employee_id: uuid.UUID
    template_code_snapshot: str
    template_name_snapshot: str
    template_version_number: int
    selection_mode: str
    status: str
    phase: str = "PRE_JOINING"  # derived by the service, set after validation
    initiated_on: date
    target_completion_date: date | None = None
    hr_coordinator_user_id: uuid.UUID | None = None
    completed_at: datetime | None = None
    completion_mode: str | None = None
    cancellation_reason: str | None = None
    reopened_count: int
    remarks: str | None = None
    mandatory_total: int = 0
    mandatory_closed: int = 0
    optional_total: int = 0
    optional_closed: int = 0
    overdue_count: int = 0
    progress_percent: int = 0
    row_version: int = 0
    tasks: list[TaskOut] = Field(default_factory=list)
    events: list[InstanceEventOut] = Field(default_factory=list)
    warnings: list[WarningDetail] = Field(default_factory=list)


# Submissions and acknowledgements.


class SubmissionBody(ApiModel):
    payload: dict[str, Any]
    on_behalf_reason: Annotated[str, Field(min_length=10, max_length=500)] | None = None


class SelfSubmissionBody(ApiModel):
    payload: dict[str, Any]


class SubmissionAcceptBody(ApiModel):
    acknowledged_warnings: list[str] = Field(default_factory=list)


class FieldComment(ApiModel):
    field: Annotated[str, Field(max_length=100)]
    comment: Annotated[str, Field(max_length=300)]


class SubmissionRejectBody(ApiModel):
    rejection_reason: Reason10
    field_comments: list[FieldComment] | None = None


class SubmissionWithdrawBody(ApiModel):
    reason: Annotated[str, Field(max_length=500)] | None = None


class SubmissionOut(ApiModel):
    id: uuid.UUID
    task_id: uuid.UUID
    section_code: str
    submission_no: int
    status: str
    submitted_field_names: list[str]
    submitted_on_behalf: bool
    submitted_at: datetime
    decided_at: datetime | None = None
    rejection_reason: str | None = None
    field_comments: list[FieldComment] | None = None
    applied_entity_refs: list[dict[str, str]] | None = None
    payload_purged_at: datetime | None = None
    row_version: int = 0


class AcknowledgementOut(ApiModel):
    id: uuid.UUID
    task_id: uuid.UUID
    acknowledgement_kind: str
    policy_version_label: str
    method: str
    acknowledged_on_behalf: bool
    acknowledged_at: datetime
