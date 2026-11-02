"""Salary component, template and assignment contracts (§16-§18, §24.4.6)."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, Field, model_validator

from app.models.salary import (
    AmountPeriod,
    ComponentCategory,
    ComponentType,
    ValueType,
)
from app.schemas.common import ApiModel

_ISO_4217_TEXT = (
    "AED AFN ALL AMD ANG AOA ARS AUD AWG AZN BAM BBD BDT BGN BHD BIF BMD BND BOB "
    "BRL BSD BTN BWP BYN BZD CAD CDF CHF CLP CNY COP CRC CUP CVE CZK DJF DKK DOP "
    "DZD EGP ERN ETB EUR FJD FKP GBP GEL GHS GIP GMD GNF GTQ GYD HKD HNL HTG HUF "
    "IDR ILS INR IQD IRR ISK JMD JOD JPY KES KGS KHR KMF KPW KRW KWD KYD KZT LAK "
    "LBP LKR LRD LSL LYD MAD MDL MGA MKD MMK MNT MOP MRU MUR MVR MWK MXN MYR MZN "
    "NAD NGN NIO NOK NPR NZD OMR PAB PEN PGK PHP PKR PLN PYG QAR RON RSD RUB RWF "
    "SAR SBD SCR SDG SEK SGD SHP SLE SOS SRD SSP STN SVC SYP SZL THB TJS TMT TND "
    "TOP TRY TTD TWD TZS UAH UGX USD UYU UZS VES VND VUV WST XAF XCD XOF XPF YER "
    "ZAR ZMW ZWL"
)
ISO_4217_CODES = frozenset(_ISO_4217_TEXT.split())
"""Active ISO 4217 codes. A pattern alone accepts ``XXX`` or ``ABC``, which would reach a
payslip header as a currency nobody can convert."""


def _iso_currency(value: str) -> str:
    if value not in ISO_4217_CODES:
        raise ValueError("must be an ISO 4217 currency code")
    return value


CurrencyCode = Annotated[str, Field(pattern=r"^[A-Z]{3}$"), AfterValidator(_iso_currency)]


class SalaryComponentCreateRequest(ApiModel):
    component_code: Annotated[str, Field(pattern=r"^[A-Z0-9][A-Z0-9_./-]{0,29}$")]
    component_name: Annotated[str, Field(min_length=2, max_length=100)]
    display_name: Annotated[str, Field(min_length=2, max_length=100)] | None = None
    """Shown to the employee; defaults to ``component_name``."""

    component_type: ComponentType = ComponentType.EARNING
    description: Annotated[str, Field(max_length=500)] | None = None
    component_category: ComponentCategory = ComponentCategory.OTHER
    value_type: ValueType
    amount_period: AmountPeriod = AmountPeriod.NOT_APPLICABLE
    percentage_reference_label: Annotated[str, Field(max_length=100)] | None = None
    validation_regex: Annotated[str, Field(max_length=200)] | None = None
    default_value_amount: Annotated[Decimal, Field(ge=0)] | None = None
    default_value_percentage: Annotated[Decimal, Field(ge=0, le=100)] | None = None
    default_value_text: Annotated[str, Field(max_length=500)] | None = None
    default_value_boolean: bool | None = None
    currency: CurrencyCode | None = None
    is_required: bool = False
    is_editable_per_employee: bool = True
    display_order: Annotated[int, Field(ge=0)] = 0

    @model_validator(mode="after")
    def validate_typed_value(self) -> SalaryComponentCreateRequest:
        if self.value_type is ValueType.FIXED_AMOUNT:
            if self.amount_period is AmountPeriod.NOT_APPLICABLE:
                raise ValueError("a fixed amount requires an amount_period")
            if not self.currency:
                raise ValueError("a fixed amount requires a currency")
        elif self.amount_period is not AmountPeriod.NOT_APPLICABLE:
            raise ValueError("amount_period applies only to FIXED_AMOUNT")
        if self.value_type is ValueType.OTHER_CONFIGURED and not self.validation_regex:
            raise ValueError("OTHER_CONFIGURED requires a validation_regex")
        return self


class SalaryComponentUpdateRequest(ApiModel):
    component_name: Annotated[str, Field(min_length=2, max_length=100)] | None = None
    display_name: Annotated[str, Field(min_length=2, max_length=100)] | None = None
    description: Annotated[str, Field(max_length=500)] | None = None
    component_category: ComponentCategory | None = None
    amount_period: AmountPeriod | None = None
    percentage_reference_label: Annotated[str, Field(max_length=100)] | None = None
    validation_regex: Annotated[str, Field(max_length=200)] | None = None
    default_value_amount: Annotated[Decimal, Field(ge=0)] | None = None
    default_value_percentage: Annotated[Decimal, Field(ge=0, le=100)] | None = None
    default_value_text: Annotated[str, Field(max_length=500)] | None = None
    default_value_boolean: bool | None = None
    currency: CurrencyCode | None = None
    is_required: bool | None = None
    is_editable_per_employee: bool | None = None
    display_order: Annotated[int, Field(ge=0)] | None = None


class SalaryComponentResponse(ApiModel):
    id: uuid.UUID
    component_code: str
    component_name: str
    display_name: str
    component_type: str
    is_system: bool
    description: str | None
    component_category: str
    value_type: str
    amount_period: str
    percentage_reference_label: str | None
    validation_regex: str | None
    default_value_amount: Decimal | None
    default_value_percentage: Decimal | None
    default_value_text: str | None
    default_value_boolean: bool | None
    currency: str | None
    is_required: bool
    is_editable_per_employee: bool
    display_order: int
    status: str
    row_version: int


class ComponentMappingInput(ApiModel):
    component_id: uuid.UUID
    default_value_amount: Annotated[Decimal, Field(ge=0)] | None = None
    default_value_percentage: Annotated[Decimal, Field(ge=0, le=100)] | None = None
    default_value_text: Annotated[str, Field(max_length=500)] | None = None
    default_value_boolean: bool | None = None
    is_required: bool = False
    is_overridable: bool = True
    display_order: Annotated[int, Field(ge=0)] = 0


class SalaryTemplateCreateRequest(ApiModel):
    template_code: Annotated[str, Field(pattern=r"^[A-Z0-9][A-Z0-9_./-]{0,29}$")]
    template_name: Annotated[str, Field(min_length=2, max_length=150)]
    description: Annotated[str, Field(max_length=1000)] | None = None
    currency: CurrencyCode
    effective_from: date | None = None
    version_change_note: Annotated[str, Field(max_length=1000)] | None = None
    mappings: list[ComponentMappingInput] = Field(default_factory=list)


class SalaryTemplateUpdateRequest(ApiModel):
    template_name: Annotated[str, Field(min_length=2, max_length=150)] | None = None
    description: Annotated[str, Field(max_length=1000)] | None = None


class SalaryTemplateResponse(ApiModel):
    id: uuid.UUID
    template_code: str
    template_name: str
    description: str | None
    is_archived: bool
    row_version: int


class TemplateVersionUpdateRequest(ApiModel):
    currency: CurrencyCode | None = None
    effective_from: date | None = None
    change_note: Annotated[str, Field(max_length=1000)] | None = None


class TemplateMappingsReplaceRequest(ApiModel):
    mappings: list[ComponentMappingInput]


class TemplatePublishRequest(TemplateVersionUpdateRequest):
    """Optional body for publish: unsaved builder state, saved in the same transaction.

    Omitted fields keep their stored value; ``mappings`` omitted keeps the stored mappings,
    an empty list clears them.
    """

    mappings: list[ComponentMappingInput] | None = None


class ComponentUsageResponse(ApiModel):
    template_version_count: int
    assignment_count: int


class TemplateVersionResponse(ApiModel):
    id: uuid.UUID
    template_id: uuid.UUID
    version_number: int
    currency: str
    status: str
    effective_from: date | None
    effective_to: date | None
    change_note: str | None
    mappings: list[dict[str, Any]] = Field(default_factory=list)
    row_version: int


class AssignmentComponentInput(ApiModel):
    component_id: uuid.UUID | None = None
    component_code: Annotated[str, Field(max_length=30)] | None = None
    component_name: Annotated[str, Field(max_length=100)] | None = None
    display_name: Annotated[str, Field(max_length=100)] | None = None
    component_type: ComponentType | None = None
    component_category: ComponentCategory | None = None
    value_type: ValueType | None = None
    amount_period: AmountPeriod | None = None
    value_amount: Annotated[Decimal, Field(ge=0)] | None = None
    value_percentage: Annotated[Decimal, Field(ge=0, le=100)] | None = None
    value_text: Annotated[str, Field(max_length=500)] | None = None
    value_boolean: bool | None = None
    override_reason: Annotated[str, Field(max_length=500)] | None = None


class SalaryAssignmentCreateRequest(ApiModel):
    template_id: uuid.UUID | None = None
    effective_from: date
    effective_to: date | None = None
    revision_reason: Annotated[str, Field(max_length=50)]
    revision_remarks: Annotated[str, Field(max_length=1000)] | None = None
    revision_letter_document_id: uuid.UUID | None = None
    backdate_reason: Annotated[str, Field(max_length=1000)] | None = None
    components: list[AssignmentComponentInput] = Field(default_factory=list)
    confirm: bool = False

    @model_validator(mode="after")
    def interval(self) -> SalaryAssignmentCreateRequest:
        if self.effective_to is not None and self.effective_to <= self.effective_from:
            raise ValueError("effective_to must be after effective_from")
        return self


class SalaryAssignmentUpdateRequest(ApiModel):
    effective_from: date | None = None
    effective_to: date | None = None
    revision_reason: Annotated[str, Field(max_length=50)] | None = None
    revision_remarks: Annotated[str, Field(max_length=1000)] | None = None
    revision_letter_document_id: uuid.UUID | None = None
    backdate_reason: Annotated[str, Field(max_length=1000)] | None = None
    components: list[AssignmentComponentInput] | None = None


class SalaryAssignmentResponse(ApiModel):
    id: uuid.UUID
    employee_id: uuid.UUID
    template_id: uuid.UUID | None
    template_version_id: uuid.UUID | None
    template_version_number: int | None
    template_code_snapshot: str | None
    template_name_snapshot: str | None
    currency: str
    assignment_status: str
    approval_state: str
    effective_from: date
    effective_to: date | None
    effective_to_source: str
    revision_reason: str
    revision_remarks: str | None
    is_backdated: bool
    predecessor_assignment_id: uuid.UUID | None
    superseded_by_assignment_id: uuid.UUID | None
    components: list[dict[str, Any]] = Field(default_factory=list)
    row_version: int


class AssignmentActionRequest(ApiModel):
    reason: Annotated[str, Field(min_length=10, max_length=1000)] | None = None


class ConfirmRequest(ApiModel):
    backdate_reason: Annotated[str, Field(min_length=10, max_length=1000)] | None = None


class ApproveRequest(ApiModel):
    confirm: bool = False


class RejectRequest(ApiModel):
    rejection_reason: Annotated[str, Field(min_length=10, max_length=1000)]


class CorrectRequest(ApiModel):
    components: list[AssignmentComponentInput]
    reason: Annotated[str, Field(min_length=20, max_length=1000)]


class ReplaceRequest(SalaryAssignmentCreateRequest):
    pass


class SalaryHistoryResponse(ApiModel):
    id: uuid.UUID
    salary_assignment_id: uuid.UUID
    related_assignment_id: uuid.UUID | None
    event_type: str
    effective_from_snapshot: date
    effective_to_before: date | None
    effective_to_after: date | None
    reason: str | None
    occurred_at: datetime


class CompareComponent(ApiModel):
    component_code: str
    component_name: str
    left_value: str | None
    right_value: str | None
    change_type: Literal["ADDED", "REMOVED", "CHANGED", "UNCHANGED"]
    left_overridden: bool = False
    right_overridden: bool = False


class SalaryCompareResponse(ApiModel):
    left_assignment_id: uuid.UUID
    right_assignment_id: uuid.UUID
    components: list[CompareComponent]


class PreviewResponse(ApiModel):
    proposed_status: str
    effective_from: date
    effective_to: date | None
    effective_to_source: str
    predecessor_assignment_id: uuid.UUID | None
    boundary_adjustments: list[dict[str, Any]] = Field(default_factory=list)
    validation_errors: list[dict[str, Any]] = Field(default_factory=list)


__all__ = [
    "ApproveRequest",
    "AssignmentActionRequest",
    "AssignmentComponentInput",
    "CompareComponent",
    "ComponentMappingInput",
    "ComponentUsageResponse",
    "ConfirmRequest",
    "CorrectRequest",
    "PreviewResponse",
    "RejectRequest",
    "ReplaceRequest",
    "SalaryAssignmentCreateRequest",
    "SalaryAssignmentResponse",
    "SalaryAssignmentUpdateRequest",
    "SalaryCompareResponse",
    "SalaryComponentCreateRequest",
    "SalaryComponentResponse",
    "SalaryComponentUpdateRequest",
    "SalaryHistoryResponse",
    "SalaryTemplateCreateRequest",
    "SalaryTemplateResponse",
    "SalaryTemplateUpdateRequest",
    "TemplateMappingsReplaceRequest",
    "TemplatePublishRequest",
    "TemplateVersionResponse",
    "TemplateVersionUpdateRequest",
]
