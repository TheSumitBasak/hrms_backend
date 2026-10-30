"""Employee identifier contracts (§11, §24.4.4)."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Annotated

from pydantic import Field

from app.core.masking import IdentifierType
from app.schemas.common import ApiModel


class IdentifierPutRequest(ApiModel):
    value: Annotated[str, Field(min_length=1, max_length=100)]
    issuing_country: Annotated[str, Field(pattern=r"^[A-Z]{2}$")] | None = None
    issue_date: date | None = None
    expiry_date: date | None = None
    issue_place: Annotated[str, Field(max_length=100)] | None = None
    duplicate_override_reason: Annotated[str, Field(min_length=10, max_length=500)] | None = None


class IdentifierResponse(ApiModel):
    id: uuid.UUID
    identifier_type: str
    masked_value: str
    is_current: bool
    issuing_country: str | None
    issue_date: date | None
    expiry_date: date | None
    verification_status: str
    verified_at: datetime | None
    created_at: datetime


class IdentifierRevealRequest(ApiModel):
    reason: Annotated[str, Field(min_length=10, max_length=500)]


class IdentifierRevealResponse(ApiModel):
    identifier_type: str
    value: str


class DuplicateCheckRequest(ApiModel):
    identifier_type: IdentifierType
    value: Annotated[str, Field(min_length=1, max_length=100)]


class DuplicateMatch(ApiModel):
    employee_id: uuid.UUID
    employee_code: str | None
    display_name: str | None
    masked_value: str


class DuplicateCheckResponse(ApiModel):
    matches: list[DuplicateMatch]


__all__ = [
    "DuplicateCheckRequest",
    "DuplicateCheckResponse",
    "DuplicateMatch",
    "IdentifierPutRequest",
    "IdentifierResponse",
    "IdentifierRevealRequest",
    "IdentifierRevealResponse",
]
