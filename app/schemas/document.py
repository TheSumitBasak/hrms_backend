"""Document contracts (§15, §24.4.5)."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import Field, field_validator

from app.schemas.common import ApiModel


class DocumentCategoryCreateRequest(ApiModel):
    category_code: Annotated[str, Field(pattern=r"^[A-Z0-9_]{2,40}$")]
    category_name: Annotated[str, Field(min_length=2, max_length=100)]
    description: Annotated[str, Field(max_length=500)] | None = None
    is_restricted: bool = False
    requires_verification: bool = True
    supports_expiry: bool = False
    allows_multiple_current: bool = False
    max_file_size_bytes: Annotated[int, Field(gt=0, le=100 * 1024 * 1024)] = 10 * 1024 * 1024
    is_mandatory_for_activation: bool = False
    retention_months_after_separation: Annotated[int, Field(ge=0, le=1200)] | None = None
    mime_types: Annotated[list[str], Field(min_length=1)] = Field(
        default_factory=lambda: ["application/pdf"]
    )

    @field_validator("category_code", mode="before")
    @classmethod
    def upper_code(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value


class DocumentCategoryUpdateRequest(ApiModel):
    category_name: Annotated[str, Field(min_length=2, max_length=100)] | None = None
    description: Annotated[str, Field(max_length=500)] | None = None
    is_restricted: bool | None = None
    requires_verification: bool | None = None
    supports_expiry: bool | None = None
    allows_multiple_current: bool | None = None
    max_file_size_bytes: Annotated[int, Field(gt=0, le=100 * 1024 * 1024)] | None = None
    is_mandatory_for_activation: bool | None = None
    retention_months_after_separation: Annotated[int, Field(ge=0, le=1200)] | None = None
    status: Literal["ACTIVE", "INACTIVE"] | None = None
    mime_types: list[str] | None = None


class DocumentCategoryResponse(ApiModel):
    id: uuid.UUID
    category_code: str
    category_name: str
    description: str | None
    is_restricted: bool
    requires_verification: bool
    supports_expiry: bool
    allows_multiple_current: bool
    max_file_size_bytes: int
    is_mandatory_for_activation: bool
    retention_months_after_separation: int | None
    status: str
    is_system_default: bool
    mime_types: list[str] = Field(default_factory=list)
    row_version: int


class DocumentInitiateRequest(ApiModel):
    category_id: uuid.UUID
    document_type: Annotated[str, Field(max_length=50)] | None = None
    document_title: Annotated[str, Field(min_length=2, max_length=200)]
    document_description: Annotated[str, Field(max_length=1000)] | None = None
    original_file_name: Annotated[str, Field(min_length=1, max_length=255)]
    declared_mime_type: Annotated[str, Field(max_length=100)]
    file_size_bytes: Annotated[int, Field(gt=0)]
    document_number: Annotated[str, Field(max_length=100)] | None = None
    issue_date: date | None = None
    expiry_date: date | None = None
    issued_by: Annotated[str, Field(max_length=200)] | None = None


class DocumentResponse(ApiModel):
    id: uuid.UUID
    employee_id: uuid.UUID
    document_category_id: uuid.UUID
    document_type: str
    document_title: str
    document_description: str | None
    original_file_name: str
    mime_type: str
    file_extension: str
    file_size_bytes: int
    checksum: str
    masked_document_number: str | None = None
    issue_date: date | None
    expiry_date: date | None
    issued_by: str | None
    verification_status: str
    rejection_reason: str | None
    upload_status: str
    version_number: int
    supersedes_document_id: uuid.UUID | None
    is_current: bool
    scan_result: str | None
    expiry_state: str = "NO_EXPIRY"
    uploaded_at: datetime | None
    row_version: int
    created_at: datetime


class DocumentUploadResponse(DocumentResponse):
    upload_url: str
    upload_expires_at: datetime


class DocumentReplaceResponse(DocumentUploadResponse):
    pass


class DownloadUrlResponse(ApiModel):
    url: str
    expires_at: datetime


class DocumentVerifyRequest(ApiModel):
    decision: Literal["VERIFIED", "REJECTED"]
    rejection_reason: Annotated[str, Field(min_length=10, max_length=500)] | None = None


class DocumentArchiveRequest(ApiModel):
    reason: Annotated[str, Field(min_length=10, max_length=500)]


__all__ = [
    "DocumentArchiveRequest",
    "DocumentCategoryCreateRequest",
    "DocumentCategoryResponse",
    "DocumentCategoryUpdateRequest",
    "DocumentInitiateRequest",
    "DocumentReplaceResponse",
    "DocumentResponse",
    "DocumentUploadResponse",
    "DocumentVerifyRequest",
    "DownloadUrlResponse",
]
