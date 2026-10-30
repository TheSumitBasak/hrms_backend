"""Request and response contracts for the job architecture module (JA §13.10).

Three conventions worth stating once.

**FTE is serialised as a JSON string** with exactly two decimals — ``"2.00"``, never ``2.0``
(JA-Q-13). A float cannot represent 0.01 exactly, and headcount arithmetic that silently
rounds is the kind of defect that only shows up once a department is over-staffed. Inbound
values accept a number or a string and are validated to at most two decimal places.

**Create and update models are separate**, and neither accepts a SYS field. ``id``,
``organization_id``, ``row_version``, the ``created_*``/``updated_*``/``archived_*`` columns
and every derived occupancy figure are response-only; ``ApiModel`` has ``extra="forbid"``, so
supplying one is a ``422`` rather than a silently dropped key (JA §19.4).

**Capacity is not editable through an update model.** ``PositionUpdateRequest`` deliberately
has no ``authorized_fte`` or ``max_incumbents``: JA §8.7 routes every post-creation change
through an approved headcount change request, and leaving the fields out of the schema is what
makes that structural rather than a check someone has to remember.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any, Literal

from pydantic import BeforeValidator, Field, PlainSerializer, field_validator, model_validator

from app.models.position import MAX_PROFICIENCY, MIN_PROFICIENCY
from app.schemas.common import AcknowledgementMixin, ApiModel

CODE_PATTERN = r"^[A-Z0-9][A-Z0-9_./-]{0,29}$"
"""§21.1 V-CODE, shared by every code in this module."""

MAX_FTE_DECIMALS = 2

NodeLevel = Literal["FAMILY", "SUBFAMILY"]
CatalogueStatusValue = Literal["ACTIVE", "INACTIVE"]
RequirementTypeValue = Literal["REQUIRED", "PREFERRED"]
PositionStatusValue = Literal["DRAFT", "OPEN", "FROZEN", "CLOSED"]
WorkModeValue = Literal["ON_SITE", "HYBRID", "REMOTE", "OTHER"]
AssignmentTypeValue = Literal["PRIMARY", "ACTING", "SECONDARY"]
AssignmentStatusValue = Literal["SCHEDULED", "ACTIVE", "ENDED", "CANCELLED"]
ScopeTypeValue = Literal["ORGANIZATION", "DEPARTMENT", "BRANCH", "WORK_LOCATION", "JOB", "GRADE"]
PlanStatusValue = Literal["DRAFT", "ACTIVE", "ARCHIVED"]
ChangeTypeValue = Literal["INCREASE", "DECREASE", "SET"]
RequestStatusValue = Literal["DRAFT", "PENDING", "APPROVED", "REJECTED", "CANCELLED"]
ReservationSourceValue = Literal["OFFER", "ONBOARDING", "TRANSFER", "MANUAL"]
ReservationStatusValue = Literal["ACTIVE", "RELEASED", "EXPIRED"]
GroupByValue = Literal["ORGANIZATION", "DEPARTMENT", "BRANCH", "JOB", "GRADE", "POSITION"]


def _quantised(value: Any) -> Any:
    """Accept a number or string FTE and reject more than two decimal places.

    Rejecting rather than rounding is deliberate (JA §8.2): silently turning 0.333 into 0.33
    would make the stored authorization differ from what the caller asked for.
    """
    if value is None or isinstance(value, Decimal):
        candidate = value
    elif isinstance(value, str | int | float):
        try:
            candidate = Decimal(str(value))
        except InvalidOperation as exc:
            raise ValueError("must be a decimal number") from exc
    else:
        return value
    if candidate is not None and -candidate.as_tuple().exponent > MAX_FTE_DECIMALS:  # type: ignore[operator]
        raise ValueError(f"at most {MAX_FTE_DECIMALS} decimal places are allowed")
    return candidate


def _serialise_fte(value: Decimal | None) -> str | None:
    return None if value is None else f"{value:.2f}"


FteIn = Annotated[Decimal, BeforeValidator(_quantised), Field(gt=Decimal("0"), le=Decimal("1.00"))]
"""An assignment or reservation FTE: a fraction of a full-time role (JA §10.8)."""

PositionFteIn = Annotated[
    Decimal, BeforeValidator(_quantised), Field(gt=Decimal("0"), le=Decimal("999.99"))
]
PlanFteIn = Annotated[
    Decimal, BeforeValidator(_quantised), Field(ge=Decimal("0"), le=Decimal("9999999.99"))
]
FteOut = Annotated[Decimal, PlainSerializer(_serialise_fte, return_type=str, when_used="json")]
"""JA-Q-13: rendered as a quoted two-decimal string so no client parses it as a float."""

Proficiency = Annotated[int, Field(ge=MIN_PROFICIENCY, le=MAX_PROFICIENCY)]
ReasonText = Annotated[str, Field(min_length=10, max_length=500)]


def _upper(value: object) -> object:
    return value.upper() if isinstance(value, str) else value


# =====================================================================================
# Job families
# =====================================================================================


class JobFamilySummary(ApiModel):
    """The parent reference embedded in a family or job response."""

    id: uuid.UUID
    family_code: str
    name: str
    node_level: str


class JobFamilyCreateRequest(AcknowledgementMixin):
    family_code: Annotated[str, Field(pattern=CODE_PATTERN)]
    name: Annotated[str, Field(min_length=2, max_length=150)]
    description: Annotated[str, Field(max_length=1000)] | None = None
    node_level: NodeLevel
    parent_job_family_id: uuid.UUID | None = None
    display_order: Annotated[int, Field(ge=0)] | None = None

    _upper_code = field_validator("family_code", mode="before")(_upper)

    @model_validator(mode="after")
    def validate_level(self) -> JobFamilyCreateRequest:
        """JA §5.3: a FAMILY has no parent and a SUBFAMILY must name one."""
        if self.node_level == "SUBFAMILY" and self.parent_job_family_id is None:
            raise ValueError("a SUBFAMILY requires parent_job_family_id")
        if self.node_level == "FAMILY" and self.parent_job_family_id is not None:
            raise ValueError("a FAMILY must not have a parent")
        return self


class JobFamilyUpdateRequest(AcknowledgementMixin):
    """Code and status are absent by design: the code is immutable and status has actions."""

    name: Annotated[str, Field(min_length=2, max_length=150)] | None = None
    description: Annotated[str, Field(max_length=1000)] | None = None
    parent_job_family_id: uuid.UUID | None = None
    display_order: Annotated[int, Field(ge=0)] | None = None


class JobFamilyResponse(ApiModel):
    id: uuid.UUID
    organization_id: uuid.UUID
    family_code: str
    name: str
    description: str | None
    node_level: str
    parent_job_family_id: uuid.UUID | None
    display_order: int | None
    status: str
    row_version: int
    archived_at: datetime | None
    parent: JobFamilySummary | None = None


# =====================================================================================
# Competencies
# =====================================================================================


class CompetencyCreateRequest(AcknowledgementMixin):
    competency_code: Annotated[str, Field(pattern=CODE_PATTERN)]
    name: Annotated[str, Field(min_length=2, max_length=150)]
    description: Annotated[str, Field(max_length=2000)] | None = None
    category_code: Annotated[str, Field(pattern=r"^[A-Z0-9_]{1,50}$")]
    status: CatalogueStatusValue = "ACTIVE"

    _upper_code = field_validator("competency_code", "category_code", mode="before")(_upper)


class CompetencyUpdateRequest(AcknowledgementMixin):
    """Status is immutable here; the explicit deactivate/reactivate actions own it."""

    name: Annotated[str, Field(min_length=2, max_length=150)] | None = None
    description: Annotated[str, Field(max_length=2000)] | None = None
    category_code: Annotated[str, Field(pattern=r"^[A-Z0-9_]{1,50}$")] | None = None

    _upper_category = field_validator("category_code", mode="before")(_upper)


class CompetencyResponse(ApiModel):
    id: uuid.UUID
    organization_id: uuid.UUID
    competency_code: str
    name: str
    description: str | None
    category_code: str
    status: str
    row_version: int
    archived_at: datetime | None


class CompetencyMappingItem(ApiModel):
    """One line of a competency replacement payload (JA §13.10)."""

    competency_id: uuid.UUID
    requirement_type: RequirementTypeValue = "REQUIRED"
    min_proficiency: Proficiency = 1
    display_order: Annotated[int, Field(ge=0)] | None = None
    effective_from: date | None = None
    effective_to: date | None = None

    @model_validator(mode="after")
    def validate_dates(self) -> CompetencyMappingItem:
        if (
            self.effective_from is not None
            and self.effective_to is not None
            and self.effective_to <= self.effective_from
        ):
            raise ValueError("effective_to must be after effective_from")
        return self


class CompetencyMappingReplaceRequest(AcknowledgementMixin):
    """A full replacement of the active mapping set.

    The service end-dates removed mappings rather than deleting them (JA §4.3), so history
    survives a replacement.
    """

    mappings: Annotated[list[CompetencyMappingItem], Field(max_length=200)] = Field(
        default_factory=list
    )
    reason: Annotated[str, Field(max_length=500)] | None = None

    @model_validator(mode="after")
    def validate_unique(self) -> CompetencyMappingReplaceRequest:
        """JA §9.4: a competency may appear once in a payload."""
        seen = {item.competency_id for item in self.mappings}
        if len(seen) != len(self.mappings):
            raise ValueError("a competency may appear only once in the payload")
        return self


class CompetencyRequirementResponse(ApiModel):
    id: uuid.UUID
    competency_id: uuid.UUID
    competency_code: str | None = None
    competency_name: str | None = None
    requirement_type: str
    min_proficiency: int
    display_order: int | None
    effective_from: date
    effective_to: date | None
    row_version: int
    source: Literal["JOB", "POSITION"] = "JOB"
    """Which level the requirement came from. A position response returns the union of job
    and position requirements (JA §9.3), and this says which is which."""


# =====================================================================================
# Job catalogue
# =====================================================================================


class GradeSummary(ApiModel):
    """A grade reference. ``rank`` is the only ordering authority (JA §6.1)."""

    id: uuid.UUID
    code: str
    name: str
    rank: int


class JobCreateRequest(AcknowledgementMixin):
    job_code: Annotated[str, Field(pattern=CODE_PATTERN)]
    title: Annotated[str, Field(min_length=2, max_length=200)]
    description: Annotated[str, Field(max_length=2000)] | None = None
    responsibilities: Annotated[str, Field(max_length=10000)] | None = None
    job_family_id: uuid.UUID
    default_grade_id: uuid.UUID | None = None
    min_grade_id: uuid.UUID | None = None
    max_grade_id: uuid.UUID | None = None
    default_designation_id: uuid.UUID | None = None
    default_employment_type_id: uuid.UUID | None = None
    min_experience_years: Annotated[int, Field(ge=0, le=50)] | None = None
    required_qualifications: Annotated[str, Field(max_length=5000)] | None = None
    status: CatalogueStatusValue = "ACTIVE"
    effective_from: date | None = None
    effective_to: date | None = None
    competencies: Annotated[list[CompetencyMappingItem], Field(max_length=200)] | None = None

    _upper_code = field_validator("job_code", mode="before")(_upper)

    @model_validator(mode="after")
    def validate_dates(self) -> JobCreateRequest:
        if (
            self.effective_from is not None
            and self.effective_to is not None
            and self.effective_to <= self.effective_from
        ):
            raise ValueError("effective_to must be after effective_from")
        return self


class JobUpdateRequest(AcknowledgementMixin):
    job_code: Annotated[str, Field(pattern=CODE_PATTERN)] | None = None
    """Accepted only while no position or assignment references the job (JA §4.5); the
    service returns ``422 FIELD_READ_ONLY`` once one does."""

    title: Annotated[str, Field(min_length=2, max_length=200)] | None = None
    description: Annotated[str, Field(max_length=2000)] | None = None
    responsibilities: Annotated[str, Field(max_length=10000)] | None = None
    job_family_id: uuid.UUID | None = None
    default_grade_id: uuid.UUID | None = None
    min_grade_id: uuid.UUID | None = None
    max_grade_id: uuid.UUID | None = None
    default_designation_id: uuid.UUID | None = None
    default_employment_type_id: uuid.UUID | None = None
    min_experience_years: Annotated[int, Field(ge=0, le=50)] | None = None
    required_qualifications: Annotated[str, Field(max_length=5000)] | None = None
    effective_from: date | None = None
    effective_to: date | None = None
    competencies: Annotated[list[CompetencyMappingItem], Field(max_length=200)] | None = None

    _upper_code = field_validator("job_code", mode="before")(_upper)


class JobSummary(ApiModel):
    id: uuid.UUID
    job_code: str
    title: str
    job_family_id: uuid.UUID
    default_grade_id: uuid.UUID | None
    status: str
    effective_from: date
    effective_to: date | None
    row_version: int


class JobResponse(ApiModel):
    id: uuid.UUID
    organization_id: uuid.UUID
    job_code: str
    title: str
    description: str | None
    responsibilities: str | None
    job_family_id: uuid.UUID
    default_grade_id: uuid.UUID | None
    min_grade_id: uuid.UUID | None
    max_grade_id: uuid.UUID | None
    default_designation_id: uuid.UUID | None
    default_employment_type_id: uuid.UUID | None
    min_experience_years: int | None
    required_qualifications: str | None
    status: str
    effective_from: date
    effective_to: date | None
    row_version: int
    archived_at: datetime | None
    family: JobFamilySummary | None = None
    default_grade: GradeSummary | None = None
    min_grade: GradeSummary | None = None
    max_grade: GradeSummary | None = None
    competencies: list[CompetencyRequirementResponse] = Field(default_factory=list)


# =====================================================================================
# Positions
# =====================================================================================


class PositionOccupancy(ApiModel):
    """The JA §8.4 figures, always derived — never a cached column."""

    as_of: date
    authorized_fte: FteOut
    occupied_fte: FteOut
    reserved_fte: FteOut
    vacant_fte: FteOut
    max_incumbents: int
    occupied_headcount: int
    reserved_headcount: int
    vacant_headcount: int
    is_fillable: bool


class PositionCreateRequest(AcknowledgementMixin):
    position_code: Annotated[str, Field(pattern=CODE_PATTERN)]
    position_title: Annotated[str, Field(min_length=2, max_length=200)] | None = None
    """Optional: defaults to the job title at creation (JA §7.2, AC-JA-06)."""

    job_id: uuid.UUID
    department_id: uuid.UUID
    sub_department_id: uuid.UUID | None = None
    branch_id: uuid.UUID | None = None
    work_location_id: uuid.UUID | None = None
    work_mode: WorkModeValue = "ON_SITE"
    business_unit_id: uuid.UUID | None = None
    cost_center_id: uuid.UUID | None = None
    grade_id: uuid.UUID | None = None
    """Optional: inherits ``job.default_grade_id`` at creation (JA §6.2)."""

    reports_to_position_id: uuid.UUID | None = None
    authorized_fte: PositionFteIn = Decimal("1.00")
    max_incumbents: Annotated[int, Field(ge=1, le=999)] = 1
    planned_start_date: date | None = None
    planned_end_date: date | None = None
    competencies: Annotated[list[CompetencyMappingItem], Field(max_length=200)] | None = None

    _upper_code = field_validator("position_code", mode="before")(_upper)

    @model_validator(mode="after")
    def validate_dates(self) -> PositionCreateRequest:
        if (
            self.planned_start_date is not None
            and self.planned_end_date is not None
            and self.planned_end_date <= self.planned_start_date
        ):
            raise ValueError("planned_end_date must be after planned_start_date")
        return self


class PositionUpdateRequest(AcknowledgementMixin):
    """Definition only; capacity is not editable here.

    ``authorized_fte`` and ``max_incumbents`` are accepted by the schema but always refused by
    the service with ``422 HEADCOUNT_CHANGE_REQUIRED`` (JA §8.7, §14.1). Declaring them is
    deliberate: dropping them from the model would make a budget edit fail as an unknown
    field, which tells the client nothing about the approval path it should use instead.
    """

    position_code: Annotated[str, Field(pattern=CODE_PATTERN)] | None = None
    position_title: Annotated[str, Field(min_length=2, max_length=200)] | None = None
    job_id: uuid.UUID | None = None
    department_id: uuid.UUID | None = None
    sub_department_id: uuid.UUID | None = None
    branch_id: uuid.UUID | None = None
    work_location_id: uuid.UUID | None = None
    work_mode: WorkModeValue | None = None
    business_unit_id: uuid.UUID | None = None
    cost_center_id: uuid.UUID | None = None
    grade_id: uuid.UUID | None = None
    reports_to_position_id: uuid.UUID | None = None
    planned_start_date: date | None = None
    planned_end_date: date | None = None
    authorized_fte: PositionFteIn | None = Field(
        default=None,
        description=(
            "Rejected with HEADCOUNT_CHANGE_REQUIRED. Capacity changes through "
            "POST /headcount-change-requests, which a second person approves."
        ),
    )
    max_incumbents: Annotated[int, Field(ge=1, le=999)] | None = Field(
        default=None,
        description=(
            "Rejected with HEADCOUNT_CHANGE_REQUIRED. Capacity changes through "
            "POST /headcount-change-requests, which a second person approves."
        ),
    )

    _upper_code = field_validator("position_code", mode="before")(_upper)


class PositionLifecycleRequest(AcknowledgementMixin):
    """Body for open / freeze / reopen / close.

    ``reason`` is mandatory for freeze and close (JA §15.2); the service enforces that, since
    one schema serves all four actions.
    """

    reason: Annotated[str, Field(min_length=10, max_length=500)] | None = None
    effective_date: date | None = None
    headcount_change_request_id: uuid.UUID | None = None
    """Required when ``require_position_headcount_approval`` is on, or when reopening a
    closed position, which JA §15.2 treats as a new authorization."""


class PositionSummary(ApiModel):
    id: uuid.UUID
    position_code: str
    position_title: str
    job_id: uuid.UUID
    job_title: str | None = None
    department_id: uuid.UUID
    branch_id: uuid.UUID | None
    grade_id: uuid.UUID | None
    position_status: str
    authorized_fte: FteOut
    occupied_fte: FteOut
    vacant_fte: FteOut
    max_incumbents: int
    occupied_headcount: int
    vacant_headcount: int
    row_version: int


class PositionResponse(ApiModel):
    id: uuid.UUID
    organization_id: uuid.UUID
    position_code: str
    position_title: str
    job_id: uuid.UUID
    department_id: uuid.UUID
    sub_department_id: uuid.UUID | None
    branch_id: uuid.UUID | None
    work_location_id: uuid.UUID | None
    work_mode: str
    business_unit_id: uuid.UUID | None
    cost_center_id: uuid.UUID | None
    grade_id: uuid.UUID | None
    reports_to_position_id: uuid.UUID | None
    authorized_fte: FteOut
    max_incumbents: int
    position_status: str
    planned_start_date: date | None
    planned_end_date: date | None
    opened_at: datetime | None
    closed_at: datetime | None
    freeze_reason: str | None
    close_reason: str | None
    row_version: int
    archived_at: datetime | None
    job: JobSummary | None = None
    grade: GradeSummary | None = None
    reports_to: PositionSummary | None = None
    occupancy: PositionOccupancy | None = None
    competencies: list[CompetencyRequirementResponse] = Field(default_factory=list)


class PositionHeadcountResponse(ApiModel):
    """``GET /positions/{id}/headcount`` (JA §13.6): counts, never incumbent identity."""

    position_id: uuid.UUID
    position_code: str
    position_status: str
    occupancy: PositionOccupancy


class PositionHistoryItem(ApiModel):
    """One row of the merged JA §13.6 history feed."""

    kind: Literal["STATUS", "HEADCOUNT", "ASSIGNMENT"]
    occurred_at: datetime
    from_status: str | None = None
    to_status: str | None = None
    reason: str | None = None
    employee_id: uuid.UUID | None = None
    fte: FteOut | None = None
    headcount: int | None = None
    request_id: uuid.UUID | None = None


# =====================================================================================
# Employee position assignments
# =====================================================================================


class PositionChangeRequest(ApiModel):
    """The additive ``position_change`` object on the existing job-change endpoint (JA §7.7).

    Omitting it leaves the job-change endpoint behaving exactly as it did before this module
    existed — the backward-compatibility requirement in JA §20.7.
    """

    position_id: uuid.UUID | None = None
    """``None`` with ``end_current_assignment=true`` ends occupancy without starting a new
    assignment, which is what a separation or a move out of a position needs."""

    assignment_type: AssignmentTypeValue = "PRIMARY"
    fte: FteIn = Decimal("1.00")
    effective_to: date | None = None
    end_current_assignment: bool = True
    reason: Annotated[str, Field(max_length=500)] | None = None

    @model_validator(mode="after")
    def validate_target(self) -> PositionChangeRequest:
        if self.position_id is None and not self.end_current_assignment:
            raise ValueError("position_id is required unless end_current_assignment is true")
        return self


class EmployeePositionAssignmentResponse(ApiModel):
    id: uuid.UUID
    employee_id: uuid.UUID
    position_id: uuid.UUID
    position_code: str | None = None
    position_title: str | None = None
    assignment_type: str
    assignment_status: str
    fte: FteOut
    effective_from: date
    effective_to: date | None
    end_reason: str | None
    source: str
    row_version: int


# =====================================================================================
# Headcount
# =====================================================================================


class HeadcountPlanCreateRequest(ApiModel):
    plan_code: Annotated[str, Field(pattern=CODE_PATTERN)]
    name: Annotated[str, Field(min_length=2, max_length=150)]
    description: Annotated[str, Field(max_length=1000)] | None = None
    scope_type: ScopeTypeValue
    department_id: uuid.UUID | None = None
    branch_id: uuid.UUID | None = None
    work_location_id: uuid.UUID | None = None
    job_id: uuid.UUID | None = None
    grade_id: uuid.UUID | None = None
    authorized_fte: PlanFteIn
    authorized_headcount: Annotated[int, Field(ge=0, le=1000000)]
    effective_from: date
    effective_to: date | None = None

    _upper_code = field_validator("plan_code", mode="before")(_upper)

    @model_validator(mode="after")
    def validate_scope(self) -> HeadcountPlanCreateRequest:
        """JA §8.6: exactly one scope, and the id must match the declared scope type."""
        expected = {
            "ORGANIZATION": None,
            "DEPARTMENT": "department_id",
            "BRANCH": "branch_id",
            "WORK_LOCATION": "work_location_id",
            "JOB": "job_id",
            "GRADE": "grade_id",
        }[self.scope_type]
        supplied = {
            name
            for name in ("department_id", "branch_id", "work_location_id", "job_id", "grade_id")
            if getattr(self, name) is not None
        }
        if expected is None and supplied:
            raise ValueError("an ORGANIZATION plan takes no scope id")
        if expected is not None and supplied != {expected}:
            raise ValueError(f"a {self.scope_type} plan requires exactly {expected}")
        if self.effective_to is not None and self.effective_to <= self.effective_from:
            raise ValueError("effective_to must be after effective_from")
        return self


class HeadcountPlanUpdateRequest(ApiModel):
    """Draft-only edits. Scope and code are immutable; create a new plan instead."""

    name: Annotated[str, Field(min_length=2, max_length=150)] | None = None
    description: Annotated[str, Field(max_length=1000)] | None = None
    authorized_fte: PlanFteIn | None = None
    authorized_headcount: Annotated[int, Field(ge=0, le=1000000)] | None = None
    effective_from: date | None = None
    effective_to: date | None = None


class HeadcountPlanResponse(ApiModel):
    id: uuid.UUID
    organization_id: uuid.UUID
    plan_code: str
    name: str
    description: str | None
    scope_type: str
    department_id: uuid.UUID | None
    branch_id: uuid.UUID | None
    work_location_id: uuid.UUID | None
    job_id: uuid.UUID | None
    grade_id: uuid.UUID | None
    authorized_fte: FteOut
    authorized_headcount: int
    plan_status: str
    effective_from: date
    effective_to: date | None
    approved_by: uuid.UUID | None
    approved_at: datetime | None
    row_version: int
    plan_authorized_fte: FteOut | None = None
    """The establishment currently committed against this plan (JA §8.4
    ``PlanAuthorizedFTE``). Present on a single-plan read."""

    plan_available_fte: FteOut | None = None
    plan_authorized_headcount: int | None = None
    plan_available_headcount: int | None = None


class HeadcountChangeRequestCreate(ApiModel):
    position_id: uuid.UUID | None = None
    headcount_plan_id: uuid.UUID | None = None
    change_type: ChangeTypeValue
    requested_fte: PlanFteIn
    requested_headcount: Annotated[int, Field(ge=0, le=1000000)]
    reason: Annotated[str, Field(min_length=20, max_length=1000)]

    @model_validator(mode="after")
    def validate_target(self) -> HeadcountChangeRequestCreate:
        """JA §8.7: exactly one target. ``current_*`` is snapshotted by the service, not sent."""
        if (self.position_id is None) == (self.headcount_plan_id is None):
            raise ValueError("supply exactly one of position_id or headcount_plan_id")
        return self


class HeadcountChangeRequestUpdate(ApiModel):
    change_type: ChangeTypeValue | None = None
    requested_fte: PlanFteIn | None = None
    requested_headcount: Annotated[int, Field(ge=0, le=1000000)] | None = None
    reason: Annotated[str, Field(min_length=20, max_length=1000)] | None = None


class HeadcountDecisionRequest(ApiModel):
    note: Annotated[str, Field(min_length=10, max_length=1000)] | None = None
    """Required to reject (JA §15.5); optional when approving."""

    effective_date: date | None = None


class HeadcountChangeRequestResponse(ApiModel):
    id: uuid.UUID
    organization_id: uuid.UUID
    position_id: uuid.UUID | None
    headcount_plan_id: uuid.UUID | None
    change_type: str
    current_fte: FteOut
    requested_fte: FteOut
    current_headcount: int
    requested_headcount: int
    reason: str
    request_status: str
    submitted_at: datetime | None
    submitted_by: uuid.UUID | None
    decided_by: uuid.UUID | None
    decided_at: datetime | None
    decision_note: str | None
    applied_at: datetime | None
    row_version: int


class HeadcountReservationCreate(ApiModel):
    reserved_fte: FteIn = Decimal("1.00")
    reserved_headcount: Annotated[int, Field(ge=1, le=999)] = 1
    source_type: ReservationSourceValue
    source_reference: Annotated[str, Field(max_length=100)] | None = None
    effective_from: date | None = None
    expires_at: datetime
    """Mandatory: JA §8.8 forbids an open-ended reservation, which would silently hold
    capacity for ever."""

    reason: Annotated[str, Field(max_length=500)] | None = None


class HeadcountReservationResponse(ApiModel):
    id: uuid.UUID
    organization_id: uuid.UUID
    position_id: uuid.UUID
    source_type: str
    source_reference: str | None
    reserved_fte: FteOut
    reserved_headcount: int
    reservation_status: str
    effective_from: date
    expires_at: datetime | None
    reason: str | None
    row_version: int


class HeadcountSummaryRow(ApiModel):
    group_by: str
    group_id: uuid.UUID | None
    group_label: str | None
    as_of: date
    authorized_fte: FteOut
    occupied_fte: FteOut
    reserved_fte: FteOut
    vacant_fte: FteOut
    authorized_headcount: int
    occupied_headcount: int
    reserved_headcount: int
    vacant_headcount: int
    position_count: int
    open_position_count: int
    over_capacity: int
    """Positions in this group whose occupancy exceeds their authorization. Must be zero; a
    non-zero value is a defect, not a business state (JA §8.10)."""

    under_capacity: int


class HeadcountReconciliationFinding(ApiModel):
    finding: str
    severity: Literal["P1", "P2"]
    position_id: uuid.UUID | None = None
    headcount_plan_id: uuid.UUID | None = None
    scope_type: str | None = None
    authorized: FteOut | None = None
    occupied: FteOut | None = None
    delta: FteOut | None = None
    detail: str | None = None


class HeadcountReconciliationResponse(ApiModel):
    as_of: date
    findings: list[HeadcountReconciliationFinding] = Field(default_factory=list)


__all__ = [
    "CODE_PATTERN",
    "CompetencyCreateRequest",
    "CompetencyMappingItem",
    "CompetencyMappingReplaceRequest",
    "CompetencyRequirementResponse",
    "CompetencyResponse",
    "CompetencyUpdateRequest",
    "EmployeePositionAssignmentResponse",
    "GradeSummary",
    "GroupByValue",
    "HeadcountChangeRequestCreate",
    "HeadcountChangeRequestResponse",
    "HeadcountChangeRequestUpdate",
    "HeadcountDecisionRequest",
    "HeadcountPlanCreateRequest",
    "HeadcountPlanResponse",
    "HeadcountPlanUpdateRequest",
    "HeadcountReconciliationFinding",
    "HeadcountReconciliationResponse",
    "HeadcountReservationCreate",
    "HeadcountReservationResponse",
    "HeadcountSummaryRow",
    "JobCreateRequest",
    "JobFamilyCreateRequest",
    "JobFamilyResponse",
    "JobFamilySummary",
    "JobFamilyUpdateRequest",
    "JobResponse",
    "JobSummary",
    "JobUpdateRequest",
    "PositionChangeRequest",
    "PositionCreateRequest",
    "PositionHeadcountResponse",
    "PositionHistoryItem",
    "PositionLifecycleRequest",
    "PositionOccupancy",
    "PositionResponse",
    "PositionSummary",
    "PositionUpdateRequest",
]
