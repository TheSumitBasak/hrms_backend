"""Bank account contracts (§14, §24.4.4)."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import Field

from app.schemas.common import AcknowledgementMixin, ApiModel


class BankAccountCreateRequest(AcknowledgementMixin):
    account_holder_name: Annotated[str, Field(min_length=2, max_length=150)]
    bank_name: Annotated[str, Field(min_length=2, max_length=150)]
    branch_name: Annotated[str, Field(max_length=150)] | None = None
    account_number: Annotated[str, Field(min_length=4, max_length=34)]
    ifsc_code: Annotated[str, Field(min_length=11, max_length=11)]
    account_type: Annotated[str, Field(max_length=50)]
    effective_from: date
    is_primary: bool = False


class BankAccountUpdateRequest(ApiModel):
    branch_name: Annotated[str, Field(max_length=150)] | None = None
    account_type: Annotated[str, Field(max_length=50)] | None = None
    effective_to: date | None = None


class BankAccountPrimaryRequest(ApiModel):
    effective_from: date


class BankAccountVerifyRequest(ApiModel):
    status: Literal["UNVERIFIED", "PENDING", "VERIFIED", "FAILED"]
    verification_date: date
    remarks: Annotated[str, Field(max_length=500)] | None = None
    bank_proof_document_id: uuid.UUID | None = None


class BankAccountRevealRequest(ApiModel):
    reason: Annotated[str, Field(min_length=10, max_length=500)]


class BankAccountResponse(ApiModel):
    id: uuid.UUID
    account_holder_name: str
    bank_name: str
    branch_name: str | None
    masked_account_number: str
    ifsc_code: str
    account_type: str
    is_primary: bool
    verification_status: str
    verification_date: date | None
    bank_proof_document_id: uuid.UUID | None
    effective_from: date
    effective_to: date | None
    row_version: int
    created_at: datetime


class BankAccountRevealResponse(ApiModel):
    account_number: str


__all__ = [
    "BankAccountCreateRequest",
    "BankAccountPrimaryRequest",
    "BankAccountResponse",
    "BankAccountRevealRequest",
    "BankAccountRevealResponse",
    "BankAccountUpdateRequest",
    "BankAccountVerifyRequest",
]
