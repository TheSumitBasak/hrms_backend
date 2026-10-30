"""Document categories and lifecycle (§15)."""

from __future__ import annotations

import hashlib
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import PurePath
from typing import Any

from app.audit.actions import AuditAction
from app.audit.writer import AuditWriter
from app.core.clock import utc_now
from app.core.context import OrgContext
from app.core.crypto import FieldCipher, FieldContext
from app.core.errors import (
    DocumentAccessDenied,
    DocumentNotAvailable,
    DuplicateResource,
    ErrorDetail,
    FileTooLarge,
    FileTypeNotAllowed,
    ValidationFailed,
)
from app.core.masking import last_four_of
from app.core.permissions import Permission
from app.db.types import uuid7
from app.models.document import (
    DocumentCategory,
    DocumentVerificationStatus,
    EmployeeDocument,
    ExpiryState,
    UploadStatus,
)
from app.repositories.document import (
    DocumentCategoryRepository,
    DocumentMimeTypeRepository,
    EmployeeDocumentRepository,
)
from app.repositories.employee import EmployeeRepository
from app.repositories.organization import OrganizationRepository, OrganizationSettingsRepository
from app.services.organizations import assert_etag
from app.storage.local import LocalStorage, SignedUrlSigner

DOCUMENT_NUMBER_MIN = 4
_UNSAFE = re.compile(r"[\\/:*?\"<>|\x00-\x1f]")
EXTENSIONS = {
    "application/pdf": "pdf",
    "image/png": "png",
    "image/jpeg": "jpg",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
}
"""Development allowlist. The category row is authoritative; this map only names extensions."""


def _sanitize_filename(name: str) -> str:
    cleaned = _UNSAFE.sub("_", PurePath(name).name).strip().strip(".")
    return (cleaned or "document")[:255]


def _detect_mime(data: bytes) -> str | None:
    if data.startswith(b"%PDF-"):
        return "application/pdf"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"PK\x03\x04"):
        return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    return None


class DocumentService:
    def __init__(
        self,
        *,
        categories: DocumentCategoryRepository,
        mime_types: DocumentMimeTypeRepository,
        documents: EmployeeDocumentRepository,
        employees: EmployeeRepository,
        organizations: OrganizationRepository,
        settings: OrganizationSettingsRepository,
        cipher: FieldCipher,
        storage: LocalStorage,
        signer: SignedUrlSigner,
        audit: AuditWriter,
        upload_ttl_seconds: int,
        download_ttl_seconds: int = 300,
    ) -> None:
        self._categories = categories
        self._mime_types = mime_types
        self._documents = documents
        self._employees = employees
        self._organizations = organizations
        self._settings = settings
        self._cipher = cipher
        self._storage = storage
        self._signer = signer
        self._audit = audit
        self._upload_ttl = upload_ttl_seconds
        self._download_ttl = download_ttl_seconds

    # ---------------------------------------------------------------- categories

    def list_categories(
        self, context: OrgContext, *, include_inactive: bool = False
    ) -> list[tuple[DocumentCategory, list[str]]]:
        context.require_any(Permission.EMPLOYEE_LIST, Permission.MASTER_MANAGE)
        return [
            (row, list(self._mime_types.list_for_category(row.id)))
            for row in self._categories.list_categories(include_inactive=include_inactive)
        ]

    def create_category(
        self, context: OrgContext, values: dict[str, Any]
    ) -> tuple[DocumentCategory, list[str]]:
        context.require(Permission.MASTER_MANAGE)
        code = str(values["category_code"])
        if self._categories.get_by_code(code) is not None:
            raise DuplicateResource("A document category with that code exists.").with_detail(
                field="category_code", code="DUPLICATE", message="Category codes are unique."
            )
        mime_types = values.pop("mime_types", [])
        row = DocumentCategory(
            id=uuid7(),
            category_code=code,
            category_name=str(values["category_name"]),
            description=values.get("description"),
            is_restricted=bool(values.get("is_restricted")),
            requires_verification=bool(values.get("requires_verification", True)),
            supports_expiry=bool(values.get("supports_expiry")),
            allows_multiple_current=bool(values.get("allows_multiple_current")),
            max_file_size_bytes=int(values.get("max_file_size_bytes", 10 * 1024 * 1024)),
            is_mandatory_for_activation=bool(values.get("is_mandatory_for_activation")),
            retention_months_after_separation=values.get("retention_months_after_separation"),
            status="ACTIVE",
            is_system_default=False,
            created_by=context.user_id,
        )
        self._categories.add(row)
        self._categories.flush()
        for mime in mime_types:
            self._mime_types.add(row.id, mime)
        self._audit.record_change(
            context,
            action=AuditAction.DOCUMENT_CATEGORY_CREATED,
            entity_type="document_category",
            entity_id=row.id,
            after={"category_code": code, "category_name": row.category_name},
            fields=["category_code", "category_name"],
        )
        return row, list(mime_types)

    def update_category(
        self,
        context: OrgContext,
        category_id: uuid.UUID,
        changes: dict[str, Any],
        *,
        if_match: int | None,
    ) -> tuple[DocumentCategory, list[str]]:
        context.require(Permission.MASTER_MANAGE)
        row = self._categories.require(category_id, resource="document category")
        assert_etag(row.row_version, if_match)
        mime_types = changes.pop("mime_types", None)
        before = {field: getattr(row, field) for field in changes}
        for field, value in changes.items():
            setattr(row, field, value)
        row.updated_by = context.user_id
        if mime_types is not None:
            self._mime_types.delete_for_category(row.id)
            for mime in mime_types:
                self._mime_types.add(row.id, mime)
        self._audit.record_change(
            context,
            action=AuditAction.DOCUMENT_CATEGORY_UPDATED,
            entity_type="document_category",
            entity_id=row.id,
            before=before,
            after={field: getattr(row, field) for field in changes},
            fields=list(changes),
        )
        return row, list(mime_types) if mime_types is not None else list(
            self._mime_types.list_for_category(row.id)
        )

    # ---------------------------------------------------------------- initiate / complete

    def initiate_upload(
        self,
        context: OrgContext,
        employee_id: uuid.UUID,
        values: dict[str, Any],
        *,
        replaces: uuid.UUID | None = None,
    ) -> tuple[EmployeeDocument, str, datetime]:
        context.require(Permission.DOCUMENT_UPLOAD)
        employee = self._employees.require(employee_id, resource="employee")
        category = self._categories.require(values["category_id"], resource="document category")
        if category.status != "ACTIVE" or category.archived_at is not None:
            raise ValidationFailed("That document category is inactive.")
        allowed = set(self._mime_types.list_for_category(category.id))
        if values["declared_mime_type"] not in allowed:
            raise FileTypeNotAllowed()
        if int(values["file_size_bytes"]) > category.max_file_size_bytes:
            raise FileTooLarge()
        if values.get("expiry_date") is not None and not category.supports_expiry:
            raise ValidationFailed(
                details=[
                    ErrorDetail("expiry_date", "NOT_SUPPORTED", "This category has no expiry.")
                ]
            )

        document_type = values.get("document_type") or category.category_code
        supersedes: EmployeeDocument | None = None
        version_number = 1
        if replaces is not None:
            supersedes = self._documents.require_for_employee(employee_id, replaces)
            if supersedes.upload_status != UploadStatus.AVAILABLE.value:
                raise DocumentNotAvailable()
            if supersedes.document_category_id != category.id:
                raise ValidationFailed("A replacement must use the same category.")
            if not supersedes.is_current:
                raise ValidationFailed("Only the current version can be replaced.")
            version_number = supersedes.version_number + 1
            document_type = supersedes.document_type

        row_id = uuid7()
        storage_key = f"org/{context.organization_id.hex}/emp/{employee.id.hex}/{row_id.hex}"
        encrypted = None
        last_four = None
        key_version = None
        if values.get("document_number"):
            encrypted, key_version = self._cipher.encrypt(
                str(values["document_number"]),
                FieldContext(
                    organization_id=context.organization_id,
                    entity_type="employee_document",
                    entity_id=row_id,
                    field="document_number",
                ),
            )
            last_four = last_four_of(str(values["document_number"]))
        row = EmployeeDocument(
            id=row_id,
            employee_id=employee.id,
            document_category_id=category.id,
            document_type=document_type,
            document_title=str(values["document_title"]),
            document_description=values.get("document_description"),
            original_file_name=_sanitize_filename(str(values["original_file_name"])),
            storage_key=storage_key,
            mime_type=str(values["declared_mime_type"]),
            file_extension=EXTENSIONS.get(str(values["declared_mime_type"]), "bin"),
            file_size_bytes=int(values["file_size_bytes"]),
            checksum="",
            document_number_encrypted=encrypted,
            document_number_last_four=last_four,
            document_number_key_version=key_version,
            issue_date=values.get("issue_date"),
            expiry_date=values.get("expiry_date"),
            issued_by=values.get("issued_by"),
            verification_status=DocumentVerificationStatus.PENDING.value,
            upload_status=UploadStatus.PENDING_UPLOAD.value,
            version_number=version_number,
            supersedes_document_id=supersedes.id if supersedes else None,
            is_current=False,
            single_current_key=None,
            created_by=context.user_id,
        )
        self._documents.add(row)
        self._documents.flush()
        token, expires = self._signer.issue(
            key=storage_key,
            purpose="upload",
            ttl_seconds=self._upload_ttl,
            organization_id=context.organization_id.hex,
        )
        self._audit.record_change(
            context,
            action=AuditAction.DOCUMENT_UPLOADED,
            entity_type="employee_document",
            entity_id=row.id,
            employee_id=employee.id,
            after={"document_title": row.document_title, "status": row.upload_status},
            fields=["document_title"],
        )
        return row, f"/api/v1/storage/upload/{token}", _as_utc(expires)

    def complete_upload(
        self, context: OrgContext, employee_id: uuid.UUID, document_id: uuid.UUID
    ) -> EmployeeDocument:
        context.require(Permission.DOCUMENT_UPLOAD)
        employee = self._employees.require(employee_id, resource="employee")
        row = self._documents.require_for_employee(employee.id, document_id)
        if row.upload_status != UploadStatus.PENDING_UPLOAD.value:
            raise ValidationFailed("This upload has already been completed.")
        if not self._storage.exists(row.storage_key):
            raise DocumentNotAvailable("The file was not uploaded.")
        data = self._storage.get(row.storage_key)
        if len(data) != row.file_size_bytes:
            raise ValidationFailed(
                details=[
                    ErrorDetail("file_size_bytes", "MISMATCH", "Uploaded size does not match.")
                ]
            )
        detected = _detect_mime(data)
        category = self._categories.require(row.document_category_id, resource="document category")
        allowed = set(self._mime_types.list_for_category(category.id))
        if detected is None or detected not in allowed:
            self._storage.delete(row.storage_key)
            row.upload_status = UploadStatus.FAILED.value
            row.scan_result = "content type rejected"
            raise FileTypeNotAllowed()
        row.mime_type = detected
        row.file_extension = EXTENSIONS.get(detected, "bin")
        row.checksum = hashlib.sha256(data).hexdigest()
        row.upload_status = UploadStatus.AVAILABLE.value
        row.scan_result = "CLEAN"
        row.uploaded_at = datetime.now(UTC)
        row.uploaded_by = context.user_id
        row.verification_status = (
            DocumentVerificationStatus.PENDING.value
            if category.requires_verification
            else DocumentVerificationStatus.NOT_REQUIRED.value
        )

        if row.supersedes_document_id is not None:
            previous = self._documents.require_for_employee(employee.id, row.supersedes_document_id)
            previous.is_current = False
            previous.single_current_key = None
            previous.updated_by = context.user_id
        row.is_current = True
        row.single_current_key = None if category.allows_multiple_current else 1
        self._documents.flush()
        self._audit.record_change(
            context,
            action=AuditAction.DOCUMENT_UPLOADED,
            entity_type="employee_document",
            entity_id=row.id,
            employee_id=employee.id,
            after={"status": row.upload_status, "mime_type": row.mime_type},
            fields=["upload_status"],
        )
        return row

    # ---------------------------------------------------------------- reads

    def list_documents(
        self,
        context: OrgContext,
        employee_id: uuid.UUID,
        *,
        category_id: uuid.UUID | None = None,
        verification_status: str | None = None,
        include_archived: bool = False,
        include_versions: bool = False,
    ) -> list[EmployeeDocument]:
        context.require_any(
            Permission.DOCUMENT_DOWNLOAD,
            Permission.IDENTITY_VIEW_MASKED,
            Permission.EMPLOYEE_EDIT,
        )
        self._employees.require(employee_id, resource="employee")
        rows = self._documents.list_for_employee(
            employee_id,
            category_id=category_id,
            verification_status=verification_status,
            include_archived=include_archived,
            include_versions=include_versions,
        )
        if not context.has(Permission.DOCUMENT_DOWNLOAD_RESTRICTED):
            restricted = {
                row.id
                for row in self._categories.list_categories(include_inactive=True)
                if row.is_restricted
            }
            rows = [row for row in rows if row.document_category_id not in restricted]
        return list(rows)

    def get_document(
        self, context: OrgContext, employee_id: uuid.UUID, document_id: uuid.UUID
    ) -> EmployeeDocument:
        context.require_any(
            Permission.DOCUMENT_DOWNLOAD,
            Permission.IDENTITY_VIEW_MASKED,
            Permission.EMPLOYEE_EDIT,
        )
        self._employees.require(employee_id, resource="employee")
        row = self._documents.require_for_employee(employee_id, document_id)
        category = self._categories.require(row.document_category_id, resource="document category")
        if category.is_restricted and not context.has(Permission.DOCUMENT_DOWNLOAD_RESTRICTED):
            raise DocumentAccessDenied()
        return row

    def download_url(
        self, context: OrgContext, employee_id: uuid.UUID, document_id: uuid.UUID
    ) -> tuple[str, datetime]:
        context.require(Permission.DOCUMENT_DOWNLOAD)
        employee = self._employees.require(employee_id, resource="employee")
        row = self._documents.require_for_employee(employee.id, document_id)
        category = self._categories.require(row.document_category_id, resource="document category")
        if category.is_restricted and not context.has(Permission.DOCUMENT_DOWNLOAD_RESTRICTED):
            raise DocumentAccessDenied()
        if row.upload_status != UploadStatus.AVAILABLE.value:
            raise DocumentNotAvailable()
        token, expires = self._signer.issue(
            key=row.storage_key,
            purpose="download",
            ttl_seconds=self._download_ttl,
            organization_id=context.organization_id.hex,
        )
        settings = self._settings.require(context.organization_id)
        if settings.audit_all_document_downloads or category.is_restricted:
            self._audit.record_access(
                context,
                action=AuditAction.DOCUMENT_DOWNLOADED,
                entity_type="employee_document",
                entity_id=row.id,
                employee_id=employee.id,
            )
        return f"/api/v1/storage/download/{token}", _as_utc(expires)

    # ---------------------------------------------------------------- transition

    def verify(
        self,
        context: OrgContext,
        employee_id: uuid.UUID,
        document_id: uuid.UUID,
        *,
        decision: str,
        rejection_reason: str | None,
    ) -> EmployeeDocument:
        context.require(Permission.DOCUMENT_VERIFY)
        row = self._documents.require_for_employee(employee_id, document_id)
        settings = self._settings.require(context.organization_id)
        if not settings.allow_document_self_verification and row.uploaded_by == context.user_id:
            raise DocumentAccessDenied("Self-verification is disabled for this organization.")
        if decision == "REJECTED" and (not rejection_reason or len(rejection_reason) < 10):
            raise ValidationFailed(
                details=[ErrorDetail("rejection_reason", "REQUIRED", "A reason is required.")]
            )
        row.verification_status = decision
        row.rejection_reason = rejection_reason if decision == "REJECTED" else None
        row.verified_by = context.user_id
        row.verified_at = datetime.now(UTC)
        row.updated_by = context.user_id
        self._audit.record_change(
            context,
            action=(
                AuditAction.DOCUMENT_VERIFIED
                if decision == "VERIFIED"
                else AuditAction.DOCUMENT_REJECTED
            ),
            entity_type="employee_document",
            entity_id=row.id,
            employee_id=employee_id,
            after={"verification_status": decision},
            fields=["verification_status"],
            reason=rejection_reason,
        )
        return row

    def archive(
        self, context: OrgContext, employee_id: uuid.UUID, document_id: uuid.UUID, reason: str
    ) -> EmployeeDocument:
        context.require(Permission.DOCUMENT_ARCHIVE)
        row = self._documents.require_for_employee(employee_id, document_id)
        row.archived_at = datetime.now(UTC)
        row.archived_by = context.user_id
        row.archive_reason = reason
        row.is_current = False
        row.single_current_key = None
        row.updated_by = context.user_id
        self._audit.record_change(
            context,
            action=AuditAction.DOCUMENT_ARCHIVED,
            entity_type="employee_document",
            entity_id=row.id,
            employee_id=employee_id,
            after={"archived": True},
            fields=["archived_at"],
            reason=reason,
        )
        return row

    def expiring(self, context: OrgContext, *, within_days: int) -> list[EmployeeDocument]:
        context.require(Permission.DOCUMENT_DOWNLOAD)
        if within_days > 365:
            raise ValidationFailed(
                details=[ErrorDetail("within_days", "TOO_LARGE", "Maximum is 365 days.")]
            )
        return list(self._documents.expiring(within_days=within_days, today=utc_now().date()))

    def expiry_warning_days(self, context: OrgContext) -> int:
        return self._settings.require(context.organization_id).document_expiry_warning_days

    @staticmethod
    def expiry_state(row: EmployeeDocument, warning_days: int) -> str:
        if row.expiry_date is None:
            return ExpiryState.NO_EXPIRY.value
        today = utc_now().date()
        if row.expiry_date < today:
            return ExpiryState.EXPIRED.value
        if row.expiry_date <= today + timedelta(days=warning_days):
            return ExpiryState.EXPIRING_SOON.value
        return ExpiryState.VALID.value


def _as_utc(epoch: int) -> datetime:
    return datetime.fromtimestamp(epoch, tz=UTC)


__all__ = ["DocumentService"]
