"""Branches, masters, lookups and custom statuses (§8, §9, §10.2, §24.4.2)."""

from __future__ import annotations

import uuid
from typing import Annotated, Literal

from pydantic import Field, field_validator

from app.models.master import CustomStatusBehaviour, EmploymentTypeBase, LookupList
from app.schemas.common import AcknowledgementMixin, ApiModel, EmailAddress

CODE_PATTERN = r"^[A-Z0-9][A-Z0-9_./-]{0,29}$"
LOOKUP_VALUE_PATTERN = r"^[A-Z0-9_]{1,50}$"
CUSTOM_STATUS_PATTERN = r"^[A-Z_]{2,30}$"


class BranchCreateRequest(AcknowledgementMixin):
    branch_code: Annotated[str, Field(pattern=CODE_PATTERN)]
    branch_name: Annotated[str, Field(min_length=2, max_length=150)]
    address_line_1: Annotated[str, Field(min_length=3, max_length=200)]
    address_line_2: Annotated[str, Field(max_length=200)] | None = None
    city: Annotated[str, Field(min_length=2, max_length=100)]
    district: Annotated[str, Field(max_length=100)] | None = None
    state: Annotated[str, Field(min_length=2, max_length=100)]
    state_code: Annotated[str, Field(pattern=r"^[0-9]{2}$")] | None = None
    country: Annotated[str, Field(pattern=r"^[A-Z]{2}$")] = "IN"
    postal_code: Annotated[str, Field(min_length=3, max_length=10)]
    official_phone: Annotated[str, Field(max_length=16)] | None = None
    official_email: EmailAddress | None = None

    gstin: Annotated[str, Field(max_length=15)] | None = None
    shops_establishments_registration_number: Annotated[str, Field(max_length=50)] | None = None
    factory_license_number: Annotated[str, Field(max_length=50)] | None = None

    @field_validator("branch_code", mode="before")
    @classmethod
    def upper_code(cls, value: str) -> str:
        return value.upper()


class BranchUpdateRequest(AcknowledgementMixin):
    branch_name: Annotated[str, Field(min_length=2, max_length=150)] | None = None
    address_line_1: Annotated[str, Field(min_length=3, max_length=200)] | None = None
    address_line_2: Annotated[str, Field(max_length=200)] | None = None
    city: Annotated[str, Field(min_length=2, max_length=100)] | None = None
    district: Annotated[str, Field(max_length=100)] | None = None
    state: Annotated[str, Field(min_length=2, max_length=100)] | None = None
    state_code: Annotated[str, Field(pattern=r"^[0-9]{2}$")] | None = None
    country: Annotated[str, Field(pattern=r"^[A-Z]{2}$")] | None = None
    postal_code: Annotated[str, Field(min_length=3, max_length=10)] | None = None
    official_phone: Annotated[str, Field(max_length=16)] | None = None
    official_email: EmailAddress | None = None


class BranchStatusRequest(AcknowledgementMixin):
    reason: Annotated[str, Field(min_length=10, max_length=500)]


class BranchResponse(ApiModel):
    id: uuid.UUID
    organization_id: uuid.UUID
    branch_code: str
    branch_name: str
    address_line_1: str
    address_line_2: str | None
    city: str
    district: str | None
    state: str
    state_code: str | None
    country: str
    postal_code: str
    official_phone: str | None
    official_email: str | None
    status: str
    row_version: int
    gstin: str | None = None
    shops_establishments_registration_number: str | None = None
    factory_license_number: str | None = None


class MasterCreateRequest(AcknowledgementMixin):
    """Common §9.1 fields plus the union of §9.2 fields.

    A union rather than nine payload types because §24.4.2 is a generic endpoint. The service
    rejects a field that does not apply to the requested master, so the union can never be
    used to set an irrelevant column.
    """

    code: Annotated[str, Field(pattern=CODE_PATTERN)]
    name: Annotated[str, Field(min_length=2, max_length=150)]
    description: Annotated[str, Field(max_length=500)] | None = None
    display_order: Annotated[int, Field(ge=0)] | None = None

    head_employee_id: uuid.UUID | None = None
    department_id: uuid.UUID | None = None
    default_grade_id: uuid.UUID | None = None
    rank: Annotated[int, Field(ge=1, le=999)] | None = None
    business_unit_id: uuid.UUID | None = None
    base_type: EmploymentTypeBase | None = None
    requires_end_date: bool | None = None
    branch_id: uuid.UUID | None = None
    is_remote: bool | None = None
    address_line_1: Annotated[str, Field(min_length=3, max_length=200)] | None = None
    address_line_2: Annotated[str, Field(max_length=200)] | None = None
    address_city: Annotated[str, Field(min_length=2, max_length=100)] | None = None
    address_district: Annotated[str, Field(max_length=100)] | None = None
    address_state: Annotated[str, Field(min_length=2, max_length=100)] | None = None
    address_state_code: Annotated[str, Field(pattern=r"^[0-9]{2}$")] | None = None
    address_country: Annotated[str, Field(pattern=r"^[A-Z]{2}$")] | None = None
    address_postal_code: Annotated[str, Field(min_length=3, max_length=10)] | None = None

    @field_validator("code", mode="before")
    @classmethod
    def upper_code(cls, value: str) -> str:
        return value.upper()


class MasterUpdateRequest(AcknowledgementMixin):
    name: Annotated[str, Field(min_length=2, max_length=150)] | None = None
    description: Annotated[str, Field(max_length=500)] | None = None
    display_order: Annotated[int, Field(ge=0)] | None = None

    head_employee_id: uuid.UUID | None = None
    department_id: uuid.UUID | None = None
    default_grade_id: uuid.UUID | None = None
    rank: Annotated[int, Field(ge=1, le=999)] | None = None
    business_unit_id: uuid.UUID | None = None
    base_type: EmploymentTypeBase | None = None
    requires_end_date: bool | None = None
    branch_id: uuid.UUID | None = None
    is_remote: bool | None = None
    address_line_1: Annotated[str, Field(min_length=3, max_length=200)] | None = None
    address_line_2: Annotated[str, Field(max_length=200)] | None = None
    address_city: Annotated[str, Field(min_length=2, max_length=100)] | None = None
    address_district: Annotated[str, Field(max_length=100)] | None = None
    address_state: Annotated[str, Field(min_length=2, max_length=100)] | None = None
    address_state_code: Annotated[str, Field(pattern=r"^[0-9]{2}$")] | None = None
    address_country: Annotated[str, Field(pattern=r"^[A-Z]{2}$")] | None = None
    address_postal_code: Annotated[str, Field(min_length=3, max_length=10)] | None = None


class MasterResponse(ApiModel):
    id: uuid.UUID
    organization_id: uuid.UUID
    code: str
    name: str
    description: str | None
    display_order: int | None
    status: str
    row_version: int

    head_employee_id: uuid.UUID | None = None
    department_id: uuid.UUID | None = None
    default_grade_id: uuid.UUID | None = None
    rank: int | None = None
    business_unit_id: uuid.UUID | None = None
    base_type: str | None = None
    requires_end_date: bool | None = None
    branch_id: uuid.UUID | None = None
    is_remote: bool | None = None
    address_line_1: str | None = None
    address_line_2: str | None = None
    address_city: str | None = None
    address_district: str | None = None
    address_state: str | None = None
    address_state_code: str | None = None
    address_country: str | None = None
    address_postal_code: str | None = None


class MasterStatusRequest(AcknowledgementMixin):
    reason: Annotated[str, Field(min_length=10, max_length=500)]


class LookupCreateRequest(ApiModel):
    list_code: LookupList
    value_code: Annotated[str, Field(pattern=LOOKUP_VALUE_PATTERN)]
    label: Annotated[str, Field(min_length=1, max_length=100)]
    display_order: Annotated[int, Field(ge=0)] = 0

    @field_validator("value_code", mode="before")
    @classmethod
    def upper_value(cls, value: str) -> str:
        return value.upper()


class LookupUpdateRequest(ApiModel):
    label: Annotated[str, Field(min_length=1, max_length=100)] | None = None
    display_order: Annotated[int, Field(ge=0)] | None = None
    status: Literal["ACTIVE", "INACTIVE"] | None = None


class LookupResponse(ApiModel):
    id: uuid.UUID
    organization_id: uuid.UUID
    list_code: str
    value_code: str
    label: str
    display_order: int
    status: str
    is_system_default: bool
    row_version: int


class CustomStatusCreateRequest(ApiModel):
    code: Annotated[str, Field(pattern=CUSTOM_STATUS_PATTERN)]
    label: Annotated[str, Field(min_length=2, max_length=100)]
    base_behaviour: CustomStatusBehaviour

    @field_validator("code", mode="before")
    @classmethod
    def upper_code(cls, value: str) -> str:
        return value.upper()


class CustomStatusUpdateRequest(ApiModel):
    label: Annotated[str, Field(min_length=2, max_length=100)] | None = None
    status: Literal["ACTIVE", "INACTIVE"] | None = None


class CustomStatusResponse(ApiModel):
    id: uuid.UUID
    organization_id: uuid.UUID
    code: str
    label: str
    base_behaviour: str
    status: str
    row_version: int


__all__ = [
    "BranchCreateRequest",
    "BranchResponse",
    "BranchStatusRequest",
    "BranchUpdateRequest",
    "CustomStatusCreateRequest",
    "CustomStatusResponse",
    "CustomStatusUpdateRequest",
    "LookupCreateRequest",
    "LookupResponse",
    "LookupUpdateRequest",
    "MasterCreateRequest",
    "MasterResponse",
    "MasterStatusRequest",
    "MasterUpdateRequest",
]
