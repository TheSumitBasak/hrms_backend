"""Repositories for document categories and employee documents (§15)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date, datetime, timedelta

from sqlalchemy import select

from app.core.errors import ResourceNotFound
from app.models.document import (
    DocumentCategory,
    DocumentCategoryMimeType,
    EmployeeDocument,
    UploadStatus,
)
from app.repositories.base import OrgScopedRepository, Repository


class DocumentCategoryRepository(OrgScopedRepository[DocumentCategory]):
    model = DocumentCategory

    def list_categories(self, *, include_inactive: bool = False) -> Sequence[DocumentCategory]:
        statement = self.select(include_archived=True)
        if not include_inactive:
            statement = statement.where(DocumentCategory.status == "ACTIVE")
        statement = statement.order_by(DocumentCategory.category_name)
        return list(self._session.execute(statement).scalars())

    def get_by_code(self, category_code: str) -> DocumentCategory | None:
        statement = self.select(include_archived=True).where(
            DocumentCategory.category_code == category_code.strip().upper()
        )
        return self._session.execute(statement).scalar_one_or_none()


class DocumentMimeTypeRepository(Repository):
    model = DocumentCategoryMimeType

    def list_for_category(self, category_id: uuid.UUID) -> Sequence[str]:
        statement = select(DocumentCategoryMimeType.mime_type).where(
            DocumentCategoryMimeType.document_category_id == category_id
        )
        return list(self._session.execute(statement).scalars())

    def add(self, category_id: uuid.UUID, mime_type: str) -> None:
        self._session.add(
            DocumentCategoryMimeType(document_category_id=category_id, mime_type=mime_type)
        )

    def delete_for_category(self, category_id: uuid.UUID) -> None:
        for row in self._session.execute(
            select(DocumentCategoryMimeType).where(
                DocumentCategoryMimeType.document_category_id == category_id
            )
        ).scalars():
            self._session.delete(row)


class EmployeeDocumentRepository(OrgScopedRepository[EmployeeDocument]):
    model = EmployeeDocument

    def list_for_employee(
        self,
        employee_id: uuid.UUID,
        *,
        category_id: uuid.UUID | None = None,
        verification_status: str | None = None,
        include_archived: bool = False,
        include_versions: bool = False,
    ) -> Sequence[EmployeeDocument]:
        statement = self.select(include_archived=include_archived).where(
            EmployeeDocument.employee_id == employee_id
        )
        if category_id is not None:
            statement = statement.where(EmployeeDocument.document_category_id == category_id)
        if verification_status is not None:
            statement = statement.where(EmployeeDocument.verification_status == verification_status)
        if not include_versions:
            statement = statement.where(EmployeeDocument.is_current.is_(True))
        statement = statement.order_by(
            EmployeeDocument.document_category_id, EmployeeDocument.version_number.desc()
        )
        return list(self._session.execute(statement).scalars())

    def require_for_employee(
        self, employee_id: uuid.UUID, document_id: uuid.UUID
    ) -> EmployeeDocument:
        row = self.get(document_id, include_archived=True)
        if row is None or row.employee_id != employee_id:
            raise ResourceNotFound("document")
        return row

    def latest_version(
        self, employee_id: uuid.UUID, category_id: uuid.UUID, document_type: str
    ) -> EmployeeDocument | None:
        statement = (
            self.select(include_archived=True)
            .where(
                EmployeeDocument.employee_id == employee_id,
                EmployeeDocument.document_category_id == category_id,
                EmployeeDocument.document_type == document_type,
            )
            .order_by(EmployeeDocument.version_number.desc())
            .limit(1)
        )
        return self._session.execute(statement).scalars().first()

    def find_duplicate_checksum(
        self, employee_id: uuid.UUID, checksum: str
    ) -> EmployeeDocument | None:
        statement = self.select(include_archived=True).where(
            EmployeeDocument.employee_id == employee_id,
            EmployeeDocument.checksum == checksum,
        )
        return self._session.execute(statement).scalars().first()

    def expiring(self, *, within_days: int, today: date) -> Sequence[EmployeeDocument]:
        statement = self.select().where(
            EmployeeDocument.is_current.is_(True),
            EmployeeDocument.upload_status == UploadStatus.AVAILABLE.value,
            EmployeeDocument.expiry_date.is_not(None),
            EmployeeDocument.expiry_date >= today,
            EmployeeDocument.expiry_date <= today + timedelta(days=within_days),
        )
        statement = statement.order_by(EmployeeDocument.expiry_date)
        return list(self._session.execute(statement).scalars())

    def pending_upload_older_than(self, moment: datetime) -> Sequence[EmployeeDocument]:
        statement = self.select(include_archived=True).where(
            EmployeeDocument.upload_status == UploadStatus.PENDING_UPLOAD.value,
            EmployeeDocument.created_at < moment,
        )
        return list(self._session.execute(statement).scalars())


__all__ = [
    "DocumentCategoryRepository",
    "DocumentMimeTypeRepository",
    "EmployeeDocumentRepository",
]
