"""Employee contracts (§10, §24.4.3)."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from app.schemas.common import AcknowledgementMixin, ApiModel, EmailAddress
from app.schemas.position import (
    EmployeePositionAssignmentResponse,
    PositionChangeRequest,
)

NAME = r"^[\w.'’\- ]{1,100}$"  # noqa: RUF001 - curly apostrophes are valid name characters
PHONE = r"^\+?[0-9][0-9 \-()]{5,15}$"


class AddressInput(ApiModel):
    line_1: Annotated[str, Field(min_length=3, max_length=200)]
    line_2: Annotated[str, Field(max_length=200)] | None = None
    city: Annotated[str, Field(min_length=2, max_length=100)]
    district: Annotated[str, Field(max_length=100)] | None = None
    state: Annotated[str, Field(min_length=2, max_length=100)]
    state_code: Annotated[str, Field(pattern=r"^[0-9]{2}$")] | None = None
    country: Annotated[str, Field(pattern=r"^[A-Z]{2}$")] = "IN"
    postal_code: Annotated[str, Field(min_length=3, max_length=10)]


class AddressResponse(AddressInput):
    derived: bool = False


class EmployeeCreateRequest(AcknowledgementMixin):
    first_name: Annotated[str, Field(pattern=NAME)]
    middle_name: Annotated[str, Field(pattern=NAME)] | None = None
    last_name: Annotated[str, Field(pattern=NAME)] | None = None
    display_name: Annotated[str, Field(max_length=200)] | None = None
    father_name: Annotated[str, Field(max_length=150)] | None = None
    mother_name: Annotated[str, Field(max_length=150)] | None = None
    spouse_name: Annotated[str, Field(max_length=150)] | None = None
    date_of_birth: date | None = None
    gender: Annotated[str, Field(max_length=50)] | None = None
    marital_status: Annotated[str, Field(max_length=50)] | None = None
    nationality: Annotated[str, Field(pattern=r"^[A-Z]{2}$")] = "IN"
    blood_group: Annotated[str, Field(max_length=50)] | None = None
    religion: Annotated[str, Field(max_length=50)] | None = None
    social_category: Annotated[str, Field(max_length=50)] | None = None
    disability_status: Literal["YES", "NO", "UNDISCLOSED"] | None = None
    disability_type: Annotated[str, Field(max_length=50)] | None = None
    disability_percentage: Annotated[float, Field(ge=0, le=100)] | None = None
    employment_transition_type: Literal["NEW_HIRE", "REHIRE", "INTER_ORG_TRANSFER"] = "NEW_HIRE"
    previous_employment_record_id: uuid.UUID | None = None

    personal_mobile: Annotated[str, Field(pattern=PHONE)] | None = None
    alternate_mobile: Annotated[str, Field(pattern=PHONE)] | None = None
    personal_email: EmailAddress | None = None
    official_email: EmailAddress | None = None
    emergency_contact_name: Annotated[str, Field(max_length=150)] | None = None
    emergency_contact_relationship: Annotated[str, Field(max_length=50)] | None = None
    emergency_contact_phone: Annotated[str, Field(pattern=PHONE)] | None = None
    emergency_contact_alternate_phone: Annotated[str, Field(pattern=PHONE)] | None = None

    current_address: AddressInput | None = None
    permanent_address: AddressInput | None = None
    permanent_address_same_as_current: bool = False

    joining_date: date | None = None
    employment_type_id: uuid.UUID | None = None
    employment_start_date: date | None = None
    employment_end_date: date | None = None
    probation_duration_months: Annotated[int, Field(ge=0, le=24)] | None = None
    probation_end_date: date | None = None
    confirmation_date: date | None = None
    designation_id: uuid.UUID | None = None
    department_id: uuid.UUID | None = None
    sub_department_id: uuid.UUID | None = None
    grade_id: uuid.UUID | None = None
    business_unit_id: uuid.UUID | None = None
    cost_center_id: uuid.UUID | None = None
    branch_id: uuid.UUID | None = None
    work_location_id: uuid.UUID | None = None
    employee_category_id: uuid.UUID | None = None
    work_mode: Literal["ON_SITE", "HYBRID", "REMOTE", "OTHER"] = "ON_SITE"
    reporting_manager_employee_id: uuid.UUID | None = None
    hr_contact_employee_id: uuid.UUID | None = None
    notice_period_days: Annotated[int, Field(ge=0, le=365)] | None = None
    source_of_hire: Annotated[str, Field(max_length=50)] | None = None
    referred_by_employee_id: uuid.UUID | None = None
    offer_letter_date: date | None = None
    appointment_letter_date: date | None = None
    remarks: Annotated[str, Field(max_length=2000)] | None = None
    employee_code: Annotated[str, Field(max_length=30)] | None = None

    @field_validator("employee_code", mode="before")
    @classmethod
    def upper_code(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    @model_validator(mode="after")
    def validate_addresses(self) -> EmployeeCreateRequest:
        if self.permanent_address_same_as_current and self.permanent_address is not None:
            raise ValueError(
                "permanent_address must be omitted when permanent_address_same_as_current is true"
            )
        if self.disability_status != "YES" and (
            self.disability_type or self.disability_percentage is not None
        ):
            raise ValueError("disability_type and percentage require disability_status = YES")
        return self

    @model_validator(mode="after")
    def validate_transition_link(self) -> EmployeeCreateRequest:
        linked = self.employment_transition_type in {"REHIRE", "INTER_ORG_TRANSFER"}
        if linked and self.previous_employment_record_id is None:
            raise ValueError(
                "previous_employment_record_id is required for REHIRE or INTER_ORG_TRANSFER"
            )
        if not linked and self.previous_employment_record_id is not None:
            raise ValueError(
                "previous_employment_record_id is only valid for REHIRE or INTER_ORG_TRANSFER"
            )
        return self


class EmployeeUpdateRequest(AcknowledgementMixin):
    first_name: Annotated[str, Field(pattern=NAME)] | None = None
    middle_name: Annotated[str, Field(pattern=NAME)] | None = None
    last_name: Annotated[str, Field(pattern=NAME)] | None = None
    display_name: Annotated[str, Field(max_length=200)] | None = None
    father_name: Annotated[str, Field(max_length=150)] | None = None
    mother_name: Annotated[str, Field(max_length=150)] | None = None
    spouse_name: Annotated[str, Field(max_length=150)] | None = None
    date_of_birth: date | None = None
    gender: Annotated[str, Field(max_length=50)] | None = None
    marital_status: Annotated[str, Field(max_length=50)] | None = None
    nationality: Annotated[str, Field(pattern=r"^[A-Z]{2}$")] | None = None
    blood_group: Annotated[str, Field(max_length=50)] | None = None
    religion: Annotated[str, Field(max_length=50)] | None = None
    social_category: Annotated[str, Field(max_length=50)] | None = None
    disability_status: Literal["YES", "NO", "UNDISCLOSED"] | None = None
    disability_type: Annotated[str, Field(max_length=50)] | None = None
    disability_percentage: Annotated[float, Field(ge=0, le=100)] | None = None

    personal_mobile: Annotated[str, Field(pattern=PHONE)] | None = None
    alternate_mobile: Annotated[str, Field(pattern=PHONE)] | None = None
    personal_email: EmailAddress | None = None
    official_email: EmailAddress | None = None
    emergency_contact_name: Annotated[str, Field(max_length=150)] | None = None
    emergency_contact_relationship: Annotated[str, Field(max_length=50)] | None = None
    emergency_contact_phone: Annotated[str, Field(pattern=PHONE)] | None = None
    emergency_contact_alternate_phone: Annotated[str, Field(pattern=PHONE)] | None = None

    current_address: AddressInput | None = None
    permanent_address: AddressInput | None = None
    permanent_address_same_as_current: bool | None = None
    remarks: Annotated[str, Field(max_length=2000)] | None = None


class EmployeeAddressesResponse(ApiModel):
    current: AddressResponse | None = None
    permanent: AddressResponse | None = None
    permanent_address_same_as_current: bool = False


class EmployeeResponse(ApiModel):
    id: uuid.UUID
    organization_id: uuid.UUID
    employee_code: str | None
    first_name: str
    middle_name: str | None
    last_name: str | None
    display_name: str | None
    full_name_normalized: str
    father_name: str | None
    mother_name: str | None
    spouse_name: str | None
    date_of_birth: date | None
    age_exception_status: str
    gender: str | None
    marital_status: str | None
    nationality: str | None
    blood_group: str | None
    religion: str | None = None
    social_category: str | None = None
    disability_status: str | None
    disability_type: str | None
    disability_percentage: float | None

    personal_mobile: str | None
    alternate_mobile: str | None
    personal_email: str | None
    official_email: str | None
    emergency_contact_name: str | None
    emergency_contact_relationship: str | None
    emergency_contact_phone: str | None
    emergency_contact_alternate_phone: str | None

    joining_date: date | None
    employment_type_id: uuid.UUID | None
    employment_start_date: date | None
    employment_end_date: date | None
    probation_duration_months: int | None
    probation_end_date: date | None
    confirmation_date: date | None
    designation_id: uuid.UUID | None
    department_id: uuid.UUID | None
    sub_department_id: uuid.UUID | None
    grade_id: uuid.UUID | None
    business_unit_id: uuid.UUID | None
    cost_center_id: uuid.UUID | None
    branch_id: uuid.UUID | None
    work_location_id: uuid.UUID | None
    employee_category_id: uuid.UUID | None
    work_mode: str
    reporting_manager_employee_id: uuid.UUID | None
    hr_contact_employee_id: uuid.UUID | None
    notice_period_days: int | None
    source_of_hire: str | None
    referred_by_employee_id: uuid.UUID | None
    offer_letter_date: date | None
    appointment_letter_date: date | None
    status: str
    status_effective_date: date | None
    status_reason: str | None
    employment_transition_type: str
    previous_employment_record_id: uuid.UUID | None
    remarks: str | None
    row_version: int
    created_at: datetime
    updated_at: datetime
    addresses: EmployeeAddressesResponse | None = None
    position_assignment: EmployeePositionAssignmentResponse | None = None
    """JA §13.7: the assignment a job change created, when ``position_change`` was supplied.

    Additive and absent otherwise, so a client that has never heard of positions sees the
    response shape it saw before (JA §20.7)."""


class EmployeeSearchResponse(ApiModel):
    items: list[EmployeeSummary]
    next_cursor: str | None = None
    total_count: int | None = None


class EmployeeSummary(ApiModel):
    id: uuid.UUID
    employee_code: str | None
    display_name: str | None
    department_id: uuid.UUID | None
    department_name: str | None = None
    designation_id: uuid.UUID | None
    designation_name: str | None = None
    branch_id: uuid.UUID | None
    status: str
    joining_date: date | None
    official_email: str | None = None
    work_mode: str | None = None
    reporting_manager_code: str | None = None
    reporting_manager_name: str | None = None


class StatusTransitionRequest(ApiModel):
    to_status: Annotated[str, Field(max_length=30)]
    status_effective_date: date
    employment_end_date: date | None = None
    status_reason: Annotated[str, Field(max_length=50)] | None = None
    remarks: Annotated[str, Field(max_length=1000)] | None = None
    is_correction: bool = False


class JobChangeRequest(AcknowledgementMixin):
    """§24.4.3 job change, with the additive JA §7.7 ``position_change`` object.

    Omitting ``position_change`` preserves the pre-existing behaviour exactly, which
    JA §20.7 requires of this endpoint.
    """

    effective_date: date
    change_type: Annotated[str, Field(max_length=50)]
    department_id: uuid.UUID | None = None
    sub_department_id: uuid.UUID | None = None
    designation_id: uuid.UUID | None = None
    grade_id: uuid.UUID | None = None
    business_unit_id: uuid.UUID | None = None
    cost_center_id: uuid.UUID | None = None
    branch_id: uuid.UUID | None = None
    work_location_id: uuid.UUID | None = None
    employment_type_id: uuid.UUID | None = None
    employee_category_id: uuid.UUID | None = None
    work_mode: Literal["ON_SITE", "HYBRID", "REMOTE", "OTHER"] | None = None
    reporting_manager_employee_id: uuid.UUID | None = None
    hr_contact_employee_id: uuid.UUID | None = None
    reason: Annotated[str, Field(max_length=500)] | None = None
    position_change: PositionChangeRequest | None = None


class JobHistoryResponse(ApiModel):
    id: uuid.UUID
    effective_date: date
    sequence_no: int
    change_type: str
    department_id: uuid.UUID | None
    designation_id: uuid.UUID | None
    branch_id: uuid.UUID | None
    work_mode: str | None
    reporting_manager_employee_id: uuid.UUID | None
    reason: str | None
    position_id: uuid.UUID | None = None
    """JA §10.9: the position held after the change, when the organization uses
    positions. ``None`` for every row written before this module existed."""

    created_at: datetime


class StatusHistoryResponse(ApiModel):
    id: uuid.UUID
    from_status: str
    to_status: str
    status_effective_date: date
    employment_end_date_snapshot: date | None
    status_reason: str | None
    remarks: str | None
    is_correction: bool
    created_at: datetime


class TransferRequest(ApiModel):
    target_joining_date: date
    target_department_id: uuid.UUID
    target_designation_id: uuid.UUID
    target_employment_type_id: uuid.UUID
    target_branch_id: uuid.UUID | None = None
    source_employment_end_date: date | None = None
    copy_categories: list[
        Literal[
            "EDUCATION",
            "PREVIOUS_EMPLOYMENT",
            "FAMILY",
            "NOMINATIONS",
            "IDENTIFIERS",
        ]
    ] = Field(default_factory=list)


class AgeExceptionDecisionRequest(ApiModel):
    decision: Literal["APPROVED", "REJECTED"]
    reason: Annotated[str, Field(min_length=10, max_length=500)]


class ArchiveRequest(ApiModel):
    reason: Annotated[str, Field(min_length=10, max_length=500)]


__all__ = [
    "AddressInput",
    "AddressResponse",
    "AgeExceptionDecisionRequest",
    "ArchiveRequest",
    "EmployeeAddressesResponse",
    "EmployeeCreateRequest",
    "EmployeeResponse",
    "EmployeeSummary",
    "EmployeeUpdateRequest",
    "JobChangeRequest",
    "JobHistoryResponse",
    "StatusHistoryResponse",
    "StatusTransitionRequest",
    "TransferRequest",
]
