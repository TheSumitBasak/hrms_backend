"""Organization and per-employee audit reads (§20, §24.4.8)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta

from app.core.context import OrgContext
from app.core.errors import ErrorDetail, ValidationFailed
from app.core.permissions import Permission
from app.models.audit import AuditLog
from app.repositories.audit import AuditLogRepository
from app.repositories.employee import EmployeeRepository


class AuditQueryService:
    """Reads the append-only audit log; there is no write path here."""

    def __init__(self, *, audit_logs: AuditLogRepository, employees: EmployeeRepository) -> None:
        self._audit_logs = audit_logs
        self._employees = employees

    def search(
        self,
        context: OrgContext,
        *,
        occurred_from: datetime | None = None,
        occurred_to: datetime | None = None,
        actor_user_id: uuid.UUID | None = None,
        actions: Sequence[str] | None = None,
        entity_type: str | None = None,
        entity_id: uuid.UUID | None = None,
        employee_id: uuid.UUID | None = None,
    ) -> Sequence[AuditLog]:
        context.require(Permission.AUDIT_VIEW)
        self._assert_range(occurred_from, occurred_to)
        rows = self._audit_logs.search(
            occurred_from=occurred_from,
            occurred_to=occurred_to,
            actor_user_id=actor_user_id,
            actions=actions,
            entity_type=entity_type,
            entity_id=entity_id,
            employee_id=employee_id,
        )
        if not context.has(Permission.SALARY_VIEW):
            rows = [row for row in rows if not row.action.startswith("SALARY_")]
        return rows

    def for_employee(
        self,
        context: OrgContext,
        employee_id: uuid.UUID,
        *,
        occurred_from: datetime | None = None,
        occurred_to: datetime | None = None,
        actions: Sequence[str] | None = None,
    ) -> Sequence[AuditLog]:
        context.require(Permission.AUDIT_VIEW)
        self._employees.require(employee_id, resource="employee")
        self._assert_range(occurred_from, occurred_to)
        rows = self._audit_logs.search(
            occurred_from=occurred_from,
            occurred_to=occurred_to,
            actions=actions,
            employee_id=employee_id,
        )
        if not context.has(Permission.SALARY_VIEW):
            rows = [row for row in rows if not row.action.startswith("SALARY_")]
        return rows

    @staticmethod
    def _assert_range(start: datetime | None, end: datetime | None) -> None:
        if start is None or end is None:
            return
        if end < start:
            raise ValidationFailed(
                details=[ErrorDetail("to", "INVALID_RANGE", "to must not precede from.")]
            )
        if end - start > timedelta(days=366):
            raise ValidationFailed(
                details=[
                    ErrorDetail(
                        "to", "RANGE_TOO_LARGE", "An audit query may span at most 366 days."
                    )
                ]
            )


__all__ = ["AuditQueryService"]
