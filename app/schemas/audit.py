"""Audit log response contracts (§24.4.8)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from app.schemas.common import ApiModel


class AuditLogResponse(ApiModel):
    id: uuid.UUID
    occurred_at: datetime
    organization_id: uuid.UUID | None
    actor_user_id: uuid.UUID
    actor_role_codes: str
    action: str
    entity_type: str
    entity_id: uuid.UUID
    employee_id: uuid.UUID | None
    changed_fields: list[Any] | None
    before_values: dict[str, Any] | None
    after_values: dict[str, Any] | None
    reason: str | None
    acknowledged_warnings: list[Any] | None
    request_id: str | None
    integrity_hash: str | None


__all__ = ["AuditLogResponse"]
