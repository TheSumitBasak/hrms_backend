"""Audit log reads (§20, §24.4.8)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import select

from app.models.audit import AuditLog
from app.repositories.base import OrgScopedRepository


class AuditLogRepository(OrgScopedRepository[AuditLog]):
    model = AuditLog

    def search(
        self,
        *,
        occurred_from: datetime | None = None,
        occurred_to: datetime | None = None,
        actor_user_id: uuid.UUID | None = None,
        actions: Sequence[str] | None = None,
        entity_type: str | None = None,
        entity_id: uuid.UUID | None = None,
        employee_id: uuid.UUID | None = None,
        limit: int = 200,
    ) -> Sequence[AuditLog]:
        statement = select(AuditLog).where(AuditLog.organization_id == self.organization_id)
        if occurred_from is not None:
            statement = statement.where(AuditLog.occurred_at >= occurred_from)
        if occurred_to is not None:
            statement = statement.where(AuditLog.occurred_at <= occurred_to)
        if actor_user_id is not None:
            statement = statement.where(AuditLog.actor_user_id == actor_user_id)
        if actions:
            statement = statement.where(AuditLog.action.in_(actions))
        if entity_type is not None:
            statement = statement.where(AuditLog.entity_type == entity_type)
        if entity_id is not None:
            statement = statement.where(AuditLog.entity_id == entity_id)
        if employee_id is not None:
            statement = statement.where(AuditLog.employee_id == employee_id)
        statement = statement.order_by(AuditLog.occurred_at.desc(), AuditLog.id.desc()).limit(limit)
        return list(self._session.execute(statement).scalars())


__all__ = ["AuditLogRepository"]
