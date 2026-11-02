"""Payroll configuration contracts (payroll addendum §4, §15.2).

Payroll money and rate fields are JSON **strings** (§1.4): a bare JSON number is refused with
``MONEY_MUST_BE_STRING`` so a float can never reach a rate or an amount (PINV-01).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BeforeValidator, Field, model_validator

from app.schemas.common import AcknowledgementMixin, ApiModel


def _string_only(value: Any) -> Any:
    if isinstance(value, (bool, int, float)):
        raise ValueError("MONEY_MUST_BE_STRING: send payroll amounts and rates as JSON strings")
    return value


RatePct = Annotated[
    Decimal,
    BeforeValidator(_string_only),
    Field(ge=0, le=100, max_digits=7, decimal_places=4),
]
"""RATE_PCT (§1.4): ``DECIMAL(7,4)`` between 0 and 100, serialized as a string."""

ProrationBasisLiteral = Literal["CALENDAR_DAYS", "FIXED_30", "WORKING_DAYS"]
InputWindowModeLiteral = Literal["SAME_MONTH", "OFFSET_CYCLE"]


class PayrollSettingsRequest(ApiModel):
    """``PUT Org/payroll-settings`` — a full replacement, so an omitted field returns to its
    §4.2 default. The three module switches (§4.1) are included because there is no other
    endpoint that sets them; they live on ``organization_settings``."""

    enable_payroll_module: bool = False
    enable_payroll_self_service: bool = False
    enable_payroll_alumni_access: bool = True

    payroll_go_live_period: date
    opening_ytd_required: bool | None = None
    """Default: true unless go-live is an April (§4.2)."""

    default_proration_basis: ProrationBasisLiteral = "CALENDAR_DAYS"
    component_rounding_default: Literal[
        "NEAREST_RUPEE", "UP_RUPEE", "DOWN_RUPEE", "TWO_DECIMALS"
    ] = "NEAREST_RUPEE"
    net_pay_rounding: Literal["NEAREST_RUPEE", "NONE"] = "NEAREST_RUPEE"
    lop_requires_finalized_attendance: bool = True
    lop_lookback_periods: Annotated[int, Field(ge=0, le=12)] = 3
    arrear_lookback_periods: Annotated[int, Field(ge=0, le=36)] = 12
    salary_hold_default_on_notice: bool = False
    deduction_limit_percent: RatePct = Decimal("50.0000")
    maker_checker_mode: Literal["STRICT"] = "STRICT"
    one_time_tds_method: Literal["INCREMENTAL_IN_MONTH", "SPREAD_REMAINING"] = (
        "INCREMENTAL_IN_MONTH"
    )
    bonus_accounting_year_start_month: Annotated[int, Field(ge=1, le=12)] = 4
    gratuity_provision_enabled: bool = False
    overtime_pay_enabled: bool = False
    maternity_pay_method: Literal["REGULAR_SALARY", "AVERAGE_DAILY_WAGE"] = "REGULAR_SALARY"
    payslip_zero_pay_generate: bool = True
    alumni_access_months: Annotated[int, Field(ge=0, le=36)] = 18
    fnf_wage_payment_working_days: Annotated[int, Field(ge=0, le=30)] = 2

    @model_validator(mode="after")
    def go_live_is_a_first(self) -> PayrollSettingsRequest:
        if self.payroll_go_live_period.day != 1:
            raise ValueError("payroll_go_live_period must be the first day of a month")
        return self


class PayrollSettingsResponse(PayrollSettingsRequest):
    id: uuid.UUID
    organization_id: uuid.UUID
    opening_ytd_required: bool
    row_version: int


class PayGroupFields(ApiModel):
    pay_group_name: Annotated[str, Field(min_length=2, max_length=100)]
    proration_basis: ProrationBasisLiteral | None = None
    input_window_mode: InputWindowModeLiteral = "SAME_MONTH"
    input_cycle_start_day: Annotated[int, Field(ge=2, le=28)] | None = None
    input_cutoff_offset_days: Annotated[int, Field(ge=0, le=15)] = 2
    target_payment_day: Annotated[int, Field(ge=1, le=28)] = 1
    pay_in_period: bool = False

    @model_validator(mode="after")
    def cycle_day_matches_mode(self) -> PayGroupFields:
        if self.input_window_mode == "OFFSET_CYCLE" and self.input_cycle_start_day is None:
            raise ValueError("input_cycle_start_day is required for OFFSET_CYCLE")
        if self.input_window_mode == "SAME_MONTH" and self.input_cycle_start_day is not None:
            raise ValueError("input_cycle_start_day applies only to OFFSET_CYCLE")
        return self


class PayGroupCreateRequest(PayGroupFields):
    pay_group_code: Annotated[str, Field(pattern=r"^[A-Z0-9][A-Z0-9_./-]{0,29}$")]
    pay_frequency: Literal["MONTHLY"] = "MONTHLY"


class PayGroupUpdateRequest(ApiModel):
    """Cycle changes apply to future periods only (§15.2); existing periods are not rewritten."""

    pay_group_name: Annotated[str, Field(min_length=2, max_length=100)] | None = None
    proration_basis: ProrationBasisLiteral | None = None
    input_window_mode: InputWindowModeLiteral | None = None
    input_cycle_start_day: Annotated[int, Field(ge=2, le=28)] | None = None
    input_cutoff_offset_days: Annotated[int, Field(ge=0, le=15)] | None = None
    target_payment_day: Annotated[int, Field(ge=1, le=28)] | None = None
    pay_in_period: bool | None = None
    status: Literal["ACTIVE", "INACTIVE"] | None = None


class PayGroupResponse(ApiModel):
    id: uuid.UUID
    pay_group_code: str
    pay_group_name: str
    pay_frequency: str
    proration_basis: str | None
    input_window_mode: str
    input_cycle_start_day: int | None
    input_cutoff_offset_days: int
    target_payment_day: int
    pay_in_period: bool
    status: str
    row_version: int


class PayrollPeriodUpdateRequest(AcknowledgementMixin):
    target_payment_date: date


class PayrollPeriodResponse(ApiModel):
    id: uuid.UUID
    pay_group_id: uuid.UUID
    period_month: date
    period_start: date
    period_end: date
    tax_year_start: int
    input_window_start: date
    input_window_end: date
    input_cutoff_at: datetime
    payment_due_date: date | None
    target_payment_date: date
    status: str
    row_version: int


class StatutoryBranchInput(ApiModel):
    branch_id: uuid.UUID
    effective_from: date
    effective_to: date | None = None
    """Exclusive (§1.4)."""

    @model_validator(mode="after")
    def interval_is_forward(self) -> StatutoryBranchInput:
        if self.effective_to is not None and self.effective_to <= self.effective_from:
            raise ValueError("effective_to must be after effective_from")
        return self


class StatutoryRegistrationSettingRequest(ApiModel):
    """``PUT Org/statutory-registration-settings/{id}`` — a full replacement (§4.5).

    Which fields apply depends on the registration's type; the service refuses the rest.
    """

    pf_contribution_rate_category: Literal["STANDARD", "REDUCED"] | None = None
    pf_exempted_trust: bool = False
    pf_trust_name: Annotated[str, Field(min_length=2, max_length=200)] | None = None
    esi_implemented_area: bool = True
    deductor_category: (
        Literal[
            "COMPANY",
            "GOVERNMENT",
            "OTHERS",
            "INDIVIDUAL_HUF",
            "FIRM",
            "LLP",
            "AOP_BOI",
            "TRUST",
            "OTHER",
        ]
        | None
    ) = None
    responsible_person_name: Annotated[str, Field(min_length=2, max_length=150)] | None = None
    responsible_person_designation: Annotated[str, Field(min_length=2, max_length=100)] | None = (
        None
    )
    responsible_person_pan: Annotated[str, Field(pattern=r"^[A-Z]{5}[0-9]{4}[A-Z]$")] | None = None
    branches: list[StatutoryBranchInput] = Field(default_factory=list, max_length=500)

    @model_validator(mode="after")
    def trust_name_iff_exempted(self) -> StatutoryRegistrationSettingRequest:
        if self.pf_exempted_trust != (self.pf_trust_name is not None):
            raise ValueError("pf_trust_name is required if and only if pf_exempted_trust is true")
        return self


class StatutoryRegistrationSettingResponse(ApiModel):
    id: uuid.UUID
    registration_id: uuid.UUID
    pf_contribution_rate_category: str | None
    pf_exempted_trust: bool
    pf_trust_name: str | None
    esi_implemented_area: bool
    deductor_category: str | None
    responsible_person_name: str | None
    responsible_person_designation: str | None
    responsible_person_pan_masked: str | None
    branches: list[StatutoryBranchInput]
    row_version: int


__all__ = [
    "PayGroupCreateRequest",
    "PayGroupResponse",
    "PayGroupUpdateRequest",
    "PayrollPeriodResponse",
    "PayrollPeriodUpdateRequest",
    "PayrollSettingsRequest",
    "PayrollSettingsResponse",
    "RatePct",
    "StatutoryBranchInput",
    "StatutoryRegistrationSettingRequest",
    "StatutoryRegistrationSettingResponse",
]
