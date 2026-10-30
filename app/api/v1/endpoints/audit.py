"""Audit log reads (§24.4.8)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.api.deps import CurrentOrg, get_audit_query_service
from app.audit.actions import HEADCOUNT_SCOPED_ACTIONS
from app.core.permissions import Permission
from app.schemas.audit import AuditLogResponse
from app.services.audit_query import AuditQueryService

REDACTED = "***restricted***"
"""JA §16.4 / JA-Q-12: the marker that replaces a headcount value a caller may not see."""


def _apply_headcount_gating(
    context: CurrentOrg, rows: list[AuditLogResponse]
) -> list[AuditLogResponse]:
    """Hide headcount before/after values from a caller without ``headcount.view``.

    ``audit.view`` says a caller may see *that* a change happened and which fields moved; it
    does not say they may read the organization's establishment numbers. The same treatment
    §20.3 gives salary events, applied to the actions JA §16.1 defines.

    The rows are Pydantic models, not ORM rows, so redaction here cannot dirty the session.
    """
    if context.has(Permission.HEADCOUNT_VIEW):
        return rows
    gated = {action.value for action in HEADCOUNT_SCOPED_ACTIONS}
    for row in rows:
        if row.action not in gated:
            continue
        if row.before_values:
            row.before_values = dict.fromkeys(row.before_values, REDACTED)
        if row.after_values:
            row.after_values = dict.fromkeys(row.after_values, REDACTED)
    return rows


router = APIRouter(prefix="/orgs/{org_id}", tags=["Audit"])


@router.get(
    "/audit-logs", response_model=list[AuditLogResponse], summary="Read the organization audit log"
)
def list_audit_logs(
    context: CurrentOrg,
    service: Annotated[AuditQueryService, Depends(get_audit_query_service)],
    occurred_from: Annotated[datetime | None, Query(alias="from")] = None,
    occurred_to: Annotated[datetime | None, Query(alias="to")] = None,
    actor: uuid.UUID | None = None,
    action: Annotated[list[str] | None, Query()] = None,
    entity_type: str | None = None,
    entity_id: uuid.UUID | None = None,
    employee_id: uuid.UUID | None = None,
) -> list[AuditLogResponse]:
    rows = service.search(
        context,
        occurred_from=occurred_from,
        occurred_to=occurred_to,
        actor_user_id=actor,
        actions=action,
        entity_type=entity_type,
        entity_id=entity_id,
        employee_id=employee_id,
    )
    return _apply_headcount_gating(context, [AuditLogResponse.model_validate(row) for row in rows])


@router.get(
    "/employees/{employee_id}/audit",
    response_model=list[AuditLogResponse],
    summary="Read one employee's audit trail",
)
def employee_audit(
    employee_id: uuid.UUID,
    context: CurrentOrg,
    service: Annotated[AuditQueryService, Depends(get_audit_query_service)],
    occurred_from: Annotated[datetime | None, Query(alias="from")] = None,
    occurred_to: Annotated[datetime | None, Query(alias="to")] = None,
    action: Annotated[list[str] | None, Query()] = None,
) -> list[AuditLogResponse]:
    rows = service.for_employee(
        context,
        employee_id,
        occurred_from=occurred_from,
        occurred_to=occurred_to,
        actions=action,
    )
    return _apply_headcount_gating(context, [AuditLogResponse.model_validate(row) for row in rows])


__all__ = ["router"]
