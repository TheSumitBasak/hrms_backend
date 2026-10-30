"""Employee ID rule contracts (§19, §24.4.7)."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from app.models.employee_code import SequenceResetPolicy, YearFormat
from app.schemas.common import ApiModel

RULE_CODE_PATTERN = r"^[A-Z0-9][A-Z0-9_./-]{0,29}$"


class IdRuleCreateRequest(ApiModel):
    rule_name: Annotated[str, Field(min_length=2, max_length=100)]
    rule_code: Annotated[str, Field(pattern=RULE_CODE_PATTERN)]
    department_id: uuid.UUID | None = None
    prefix: Annotated[str, Field(pattern=r"^[A-Z0-9]{1,10}$")] | None = None
    suffix: Annotated[str, Field(pattern=r"^[A-Z0-9]{1,10}$")] | None = None
    format_pattern: Annotated[str, Field(min_length=1, max_length=100)]
    separator: Annotated[str, Field(pattern=r"^[-/_]$")] | None = None
    year_format: YearFormat | None = None
    sequence_reset_policy: SequenceResetPolicy = SequenceResetPolicy.NEVER
    sequence_group_code: Annotated[str, Field(pattern=RULE_CODE_PATTERN)] | None = None
    sequence_start: Annotated[int, Field(ge=0)] = 1
    sequence_increment: Annotated[int, Field(ge=1, le=100)] = 1
    sequence_padding_length: Annotated[int, Field(ge=1, le=10)] = 4
    effective_from: date
    effective_to: date | None = None
    priority: Annotated[int, Field(ge=1, le=999)] = 100

    @field_validator("rule_code", "sequence_group_code", mode="before")
    @classmethod
    def upper_code(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    @model_validator(mode="after")
    def validate_interval(self) -> IdRuleCreateRequest:
        if self.effective_to is not None and self.effective_to <= self.effective_from:
            raise ValueError("effective_to must be after effective_from")
        return self


class IdRuleUpdateRequest(ApiModel):
    rule_name: Annotated[str, Field(min_length=2, max_length=100)] | None = None
    prefix: Annotated[str, Field(pattern=r"^[A-Z0-9]{1,10}$")] | None = None
    suffix: Annotated[str, Field(pattern=r"^[A-Z0-9]{1,10}$")] | None = None
    format_pattern: Annotated[str, Field(min_length=1, max_length=100)] | None = None
    separator: Annotated[str, Field(pattern=r"^[-/_]$")] | None = None
    year_format: YearFormat | None = None
    sequence_reset_policy: SequenceResetPolicy | None = None
    sequence_group_code: Annotated[str, Field(pattern=RULE_CODE_PATTERN)] | None = None
    sequence_start: Annotated[int, Field(ge=0)] | None = None
    sequence_increment: Annotated[int, Field(ge=1, le=100)] | None = None
    sequence_padding_length: Annotated[int, Field(ge=1, le=10)] | None = None
    effective_from: date | None = None
    effective_to: date | None = None
    priority: Annotated[int, Field(ge=1, le=999)] | None = None
    department_id: uuid.UUID | None = None

    @field_validator("sequence_group_code", mode="before")
    @classmethod
    def upper_code(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value


class IdRuleResponse(ApiModel):
    id: uuid.UUID
    organization_id: uuid.UUID
    rule_code: str
    rule_name: str
    department_id: uuid.UUID | None
    prefix: str | None
    suffix: str | None
    format_pattern: str
    separator: str | None
    year_format: str | None
    include_organization_code: bool
    include_department_code: bool
    include_year: bool
    sequence_reset_policy: str
    sequence_group_code: str
    sequence_start: int
    sequence_increment: int
    sequence_padding_length: int
    last_issued_sequence: int | None
    effective_from: date
    effective_to: date | None
    priority: int
    status: str
    row_version: int


class IdRuleStatusRequest(ApiModel):
    reason: Annotated[str, Field(min_length=10, max_length=500)]


class PreviewRequest(ApiModel):
    department_id: uuid.UUID | None = None
    basis_date: date | None = None


class PreviewResponse(ApiModel):
    selected_rule: IdRuleResponse | None
    indicative_code: str
    indicative: Literal[True] = True


class CodeReassignmentRequest(ApiModel):
    mode: Literal["REGENERATE", "MANUAL"]
    new_code: Annotated[str, Field(pattern=r"^[A-Z0-9][A-Z0-9/_-]{0,29}$")] | None = None
    reason: Annotated[str, Field(min_length=20, max_length=500)]

    @field_validator("new_code", mode="before")
    @classmethod
    def upper_code(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    @model_validator(mode="after")
    def manual_requires_code(self) -> CodeReassignmentRequest:
        if self.mode == "MANUAL" and not self.new_code:
            raise ValueError("new_code is required when mode is MANUAL")
        return self


class IdSequenceResponse(ApiModel):
    id: uuid.UUID
    sequence_group_code: str
    scope_key: str
    last_issued_value: int


class CodeRegistryResponse(ApiModel):
    id: uuid.UUID
    employee_code_normalized: str
    employee_id: uuid.UUID
    source: str
    rule_id: uuid.UUID | None
    status: str
    issued_at: datetime
    retired_at: datetime | None
    retired_reason: str | None


__all__ = [
    "CodeReassignmentRequest",
    "CodeRegistryResponse",
    "IdRuleCreateRequest",
    "IdRuleResponse",
    "IdRuleStatusRequest",
    "IdRuleUpdateRequest",
    "IdSequenceResponse",
    "PreviewRequest",
    "PreviewResponse",
]
