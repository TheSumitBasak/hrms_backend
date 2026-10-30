"""Repositories for privacy notices, erasure and retention batches (§25)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from app.core.errors import ResourceNotFound
from app.models.privacy import (
    EmployeeErasureRequest,
    EmployeePrivacyNotice,
    RetentionPurgeBatch,
)
from app.repositories.base import OrgScopedRepository


class PrivacyNoticeRepository(OrgScopedRepository[EmployeePrivacyNotice]):
    model = EmployeePrivacyNotice

    def list_for_employee(self, employee_id: uuid.UUID) -> Sequence[EmployeePrivacyNotice]:
        statement = (
            self.select()
            .where(EmployeePrivacyNotice.employee_id == employee_id)
            .order_by(EmployeePrivacyNotice.delivered_at.desc())
        )
        return list(self._session.execute(statement).scalars())

    def require_for_employee(
        self, employee_id: uuid.UUID, notice_id: uuid.UUID
    ) -> EmployeePrivacyNotice:
        row = self.get(notice_id)
        if row is None or row.employee_id != employee_id:
            raise ResourceNotFound("privacy notice")
        return row


class ErasureRequestRepository(OrgScopedRepository[EmployeeErasureRequest]):
    model = EmployeeErasureRequest

    def list_requests(self) -> Sequence[EmployeeErasureRequest]:
        statement = self.select().order_by(EmployeeErasureRequest.requested_at.desc())
        return list(self._session.execute(statement).scalars())

    def list_for_employee(self, employee_id: uuid.UUID) -> Sequence[EmployeeErasureRequest]:
        statement = (
            self.select()
            .where(EmployeeErasureRequest.employee_id == employee_id)
            .order_by(EmployeeErasureRequest.requested_at.desc())
        )
        return list(self._session.execute(statement).scalars())

    def require_for_employee(
        self, employee_id: uuid.UUID, request_id: uuid.UUID
    ) -> EmployeeErasureRequest:
        row = self.get(request_id)
        if row is None or row.employee_id != employee_id:
            raise ResourceNotFound("erasure request")
        return row


class RetentionBatchRepository(OrgScopedRepository[RetentionPurgeBatch]):
    model = RetentionPurgeBatch

    def list_batches(self, *, status: str | None = None) -> Sequence[RetentionPurgeBatch]:
        statement = self.select(include_archived=True)
        if status is not None:
            statement = statement.where(RetentionPurgeBatch.status == status)
        statement = statement.order_by(RetentionPurgeBatch.proposed_at.desc())
        return list(self._session.execute(statement).scalars())

    def approved_unexecuted(self) -> Sequence[RetentionPurgeBatch]:
        statement = self.select(include_archived=True).where(
            RetentionPurgeBatch.status == "APPROVED"
        )
        return list(self._session.execute(statement).scalars())


__all__ = [
    "ErasureRequestRepository",
    "PrivacyNoticeRepository",
    "RetentionBatchRepository",
]
