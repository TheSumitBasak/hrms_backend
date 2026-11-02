"""Statutory rule framework contracts (payroll addendum §4.6, §15.2)."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BeforeValidator, Field, model_validator

from app.schemas.common import ApiModel
from app.schemas.payroll import RatePct, _string_only

ValueType = Literal["DECIMAL", "MONEY", "RATE_PCT", "INTEGER", "BOOLEAN", "DATE", "TEXT", "CODE"]
Verification = Literal["UNVERIFIED", "SECONDARY_CONFIRMED", "PRIMARY_CONFIRMED", "LEGALLY_APPROVED"]
Money = Annotated[
    Decimal,
    BeforeValidator(_string_only),
    Field(ge=0, max_digits=15, decimal_places=2),
]
"""MONEY (§1.4): ``DECIMAL(15,2)``, sent as a JSON string, never a number."""


class RuleParameterInput(ApiModel):
    parameter_code: Annotated[str, Field(pattern=r"^[A-Z0-9_]{1,60}$")]
    value_type: ValueType
    value: str | bool
    """A string for every type but BOOLEAN; numbers are strings (PINV-01)."""


class RuleSlabInput(ApiModel):
    slab_table_code: Annotated[str, Field(pattern=r"^[A-Z0-9_]{1,40}$")]
    sequence_no: Annotated[int, Field(ge=1, le=1000)]
    condition_code: Annotated[str, Field(max_length=40)] = ""
    lower_bound: Money
    upper_bound: Money | None = None
    rate_pct: RatePct | None = None
    fixed_amount: Money | None = None

    @model_validator(mode="after")
    def band_is_valid(self) -> RuleSlabInput:
        if self.rate_pct is None and self.fixed_amount is None:
            raise ValueError("a slab needs a rate_pct or a fixed_amount")
        if self.upper_bound is not None and self.upper_bound <= self.lower_bound:
            raise ValueError("upper_bound must be greater than lower_bound")
        return self


class RuleListValueInput(ApiModel):
    list_code: Annotated[str, Field(pattern=r"^[A-Z0-9_]{1,40}$")]
    value_code: Annotated[str, Field(min_length=1, max_length=100)]
    label: Annotated[str, Field(min_length=1, max_length=200)]


class RuleVersionDraftRequest(ApiModel):
    effective_from: date
    effective_to: date | None = None
    parameter_schema_version: Annotated[str, Field(pattern=r"^[A-Z_]+\.v[1-9][0-9]*$")]
    legal_review_notes: Annotated[str, Field(max_length=5000)] | None = None
    parameters: list[RuleParameterInput] = Field(default_factory=list, max_length=500)
    slabs: list[RuleSlabInput] = Field(default_factory=list, max_length=2000)
    list_values: list[RuleListValueInput] = Field(default_factory=list, max_length=2000)

    @model_validator(mode="after")
    def interval_is_forward(self) -> RuleVersionDraftRequest:
        if self.effective_to is not None and self.effective_to <= self.effective_from:
            raise ValueError("effective_to must be after effective_from")
        return self


class RuleVersionPublishRequest(ApiModel):
    verification_status: Verification
    legal_review_notes: Annotated[str, Field(max_length=5000)] | None = None


class RuleParameterResponse(ApiModel):
    parameter_code: str
    value_type: str
    value_decimal: Decimal | None
    value_text: str | None
    value_boolean: bool | None
    value_date: date | None


class RuleSlabResponse(ApiModel):
    slab_table_code: str
    sequence_no: int
    condition_code: str
    lower_bound: Decimal
    upper_bound: Decimal | None
    rate_pct: Decimal | None
    fixed_amount: Decimal | None


class RuleListValueResponse(ApiModel):
    list_code: str
    value_code: str
    label: str


class RuleVersionResponse(ApiModel):
    id: uuid.UUID
    rule_set_id: uuid.UUID
    version_number: int
    effective_from: date
    effective_to: date | None
    status: str
    parameter_schema_version: str
    verification_status: str
    legal_review_notes: str | None
    prepared_by: uuid.UUID
    published_by: uuid.UUID | None
    published_at: datetime | None
    parameters: list[RuleParameterResponse]
    slabs: list[RuleSlabResponse]
    list_values: list[RuleListValueResponse]


class RuleSetResponse(ApiModel):
    id: uuid.UUID
    rule_family: str
    jurisdiction_code: str
    rule_set_code: str
    description: str | None
    versions: list[RuleVersionResponse]


__all__ = [
    "RuleListValueInput",
    "RuleParameterInput",
    "RuleSetResponse",
    "RuleSlabInput",
    "RuleVersionDraftRequest",
    "RuleVersionPublishRequest",
    "RuleVersionResponse",
]
