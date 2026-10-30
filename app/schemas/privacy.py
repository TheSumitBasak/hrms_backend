"""Privacy contracts (§25)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import Field

from app.schemas.common import ApiModel


class NoticeCreateRequest(ApiModel):
    notice_version: Annotated[str, Field(min_length=1, max_length=20)]
    basis: Literal["NOTICE_ONLY", "CONSENT"]
    consent_scope: Annotated[str, Field(max_length=200)] | None = None
    delivered_at: datetime
    method: Literal["ELECTRONIC", "PAPER_UPLOADED"] = "ELECTRONIC"
    document_id: uuid.UUID | None = None


class NoticeWithdrawRequest(ApiModel):
    reason: Annotated[str, Field(min_length=10, max_length=500)] | None = None


class NoticeResponse(ApiModel):
    id: uuid.UUID
    employee_id: uuid.UUID
    notice_version: str
    basis: str
    consent_scope: str | None
    delivered_at: datetime
    acknowledged_at: datetime | None
    withdrawn_at: datetime | None
    method: str
    document_id: uuid.UUID | None
    row_version: int


class LegalHoldRequest(ApiModel):
    legal_hold: bool
    reason: Annotated[str, Field(min_length=10, max_length=500)]


class ExportRequest(ApiModel):
    unmasked: bool = False
    include_documents: bool = False


class ExportResponse(ApiModel):
    export_id: uuid.UUID
    download_url: str
    expires_at: datetime
    one_time_password: str
    masked: bool


class ErasureCreateRequest(ApiModel):
    requester_note: Annotated[str, Field(max_length=1000)] | None = None


class ErasureDecisionRequest(ApiModel):
    decision: Literal["APPROVED", "REFUSED"]
    reason: Annotated[str, Field(max_length=1000)] | None = None


class ErasureResponse(ApiModel):
    id: uuid.UUID
    employee_id: uuid.UUID
    requested_at: datetime
    requester_note: str | None
    status: str
    reviewed_by: uuid.UUID | None
    reviewed_at: datetime | None
    refusal_reason: str | None
    completed_at: datetime | None
    anonymized_fields: list[Any] | None
    row_version: int


class RetentionProposeRequest(ApiModel):
    scope_description: Annotated[str, Field(min_length=10, max_length=2000)]
    affected_entity_ids: list[uuid.UUID]


class RetentionApproveRequest(ApiModel):
    second_approver_id: uuid.UUID | None = None


class RetentionBatchResponse(ApiModel):
    id: uuid.UUID
    proposed_at: datetime
    status: str
    scope_description: str
    affected_record_count: int
    affected_entity_ids: list[Any] | None
    approved_by: uuid.UUID | None
    second_approver_id: uuid.UUID | None
    approved_at: datetime | None
    executed_at: datetime | None


class PrivacyAuditResponse(ApiModel):
    processed: int
    message: str


__all__ = [
    "ErasureCreateRequest",
    "ErasureDecisionRequest",
    "ErasureResponse",
    "ExportRequest",
    "ExportResponse",
    "LegalHoldRequest",
    "NoticeCreateRequest",
    "NoticeResponse",
    "NoticeWithdrawRequest",
    "PrivacyAuditResponse",
    "RetentionApproveRequest",
    "RetentionBatchResponse",
    "RetentionProposeRequest",
]
