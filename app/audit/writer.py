"""Persists audit events inside the caller's transaction (§20.1, NFR-10).

The writer takes the *same* :class:`~sqlalchemy.orm.Session` the service is using and only
``add``s to it — it never commits. So a business change and its audit row are committed by one
``unit_of_work`` or rolled back together, which is what NFR-10 and AC-21 require and what no
amount of "remember to log it" discipline can guarantee.

Two safety behaviours worth naming:

**The payload is re-checked before insert.** :func:`app.audit.events.build_change_set` already
redacts per §20.3, but this table is append-only: a leak committed here cannot be corrected by
an update, only by a retention purge. A second check costs nothing against that.

**A missing reason is a hard failure.** §20.1 makes ``reason`` mandatory for reveals,
overrides, corrections, break-glass and code reassignment. :class:`AuditEvent` enforces it at
construction, so an audit row for one of those actions cannot exist without one.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.actions import AuditAction
from app.audit.events import (
    AuditEvent,
    ChangeSet,
    assert_audit_payload_is_safe,
    build_change_set,
    format_actor_roles,
)
from app.core.clock import utc_now
from app.core.context import OrgContext
from app.models.audit import AuditLog


class AuditWriter:
    """Writes :class:`AuditLog` rows into an ongoing transaction.

    One per request, constructed with the request's session. Services receive it by injection
    rather than importing a module-level function, so a test can assert exactly which events a
    use case emitted.
    """

    __slots__ = ("_chain_hashes", "_session")

    def __init__(self, session: Session, *, chain_hashes: bool = False) -> None:
        self._session = session
        self._chain_hashes = chain_hashes

    def record(self, event: AuditEvent) -> AuditLog:
        """Append one audit row. Returns the pending row, not yet committed."""
        changes = event.changes or ChangeSet()
        before = dict(changes.before_values)
        after = dict(changes.after_values)

        # §20.3 backstop. Raising here aborts the whole use case, which is the correct
        # outcome: better to fail a write than to permanently record personal data that was
        # supposed to be redacted.
        assert_audit_payload_is_safe(before)
        assert_audit_payload_is_safe(after)

        row = AuditLog(
            id=uuid.uuid4(),
            occurred_at=event.occurred_at or utc_now(),
            organization_id=event.organization_id,
            actor_user_id=event.actor_user_id,
            actor_role_codes=event.actor_role_codes,
            action=event.action.value,
            entity_type=event.entity_type,
            entity_id=event.entity_id,
            employee_id=event.employee_id,
            changed_fields=changes.changed_fields or None,
            before_values=before or None,
            after_values=after or None,
            reason=event.reason,
            acknowledged_warnings=event.acknowledged_warnings or None,
            request_id=event.request_id,
            ip_address=event.ip_address,
            user_agent=event.user_agent,
        )
        row.integrity_hash = self._record_hash(row)
        if self._chain_hashes and event.organization_id is not None:
            row.previous_hash = self._latest_hash(event.organization_id)
            row.integrity_hash = self._record_hash(row, previous=row.previous_hash)

        self._session.add(row)
        return row

    def record_all(self, events: list[AuditEvent]) -> list[AuditLog]:
        """Append several events, preserving order."""
        return [self.record(event) for event in events]

    # ---------------------------------------------------------------- convenience

    def record_change(
        self,
        context: OrgContext,
        *,
        action: AuditAction,
        entity_type: str,
        entity_id: uuid.UUID,
        before: dict[str, Any] | None = None,
        after: dict[str, Any] | None = None,
        fields: list[str] | None = None,
        employee_id: uuid.UUID | None = None,
        reason: str | None = None,
        acknowledged_warnings: list[str] | None = None,
    ) -> AuditLog:
        """Build and append a field-diff audit row from an :class:`OrgContext`.

        The common path. Takes the actor, organization and correlation metadata from the
        context so a call site cannot accidentally attribute an action to the wrong user or
        omit the request id.
        """
        return self.record(
            AuditEvent(
                action=action,
                entity_type=entity_type,
                entity_id=entity_id,
                actor_user_id=context.user_id,
                actor_role_codes=format_actor_roles(context.roles),
                organization_id=context.organization_id,
                employee_id=employee_id,
                changes=build_change_set(before, after, fields=fields),
                reason=reason,
                acknowledged_warnings=acknowledged_warnings or [],
                request_id=context.metadata.request_id,
                ip_address=context.metadata.ip_address,
                user_agent=context.metadata.user_agent,
            )
        )

    def record_access(
        self,
        context: OrgContext,
        *,
        action: AuditAction,
        entity_type: str,
        entity_id: uuid.UUID,
        employee_id: uuid.UUID | None = None,
        reason: str | None = None,
    ) -> AuditLog:
        """Append a row for a sensitive *read* (§20.2 "Sensitive access").

        Carries no before/after values: a reveal changes nothing, and recording the revealed
        value is precisely what §20.3 forbids.
        """
        return self.record(
            AuditEvent(
                action=action,
                entity_type=entity_type,
                entity_id=entity_id,
                actor_user_id=context.user_id,
                actor_role_codes=format_actor_roles(context.roles),
                organization_id=context.organization_id,
                employee_id=employee_id,
                reason=reason,
                request_id=context.metadata.request_id,
                ip_address=context.metadata.ip_address,
                user_agent=context.metadata.user_agent,
            )
        )

    def record_break_glass_access(
        self, context: OrgContext, *, entity_type: str, entity_id: uuid.UUID
    ) -> AuditLog | None:
        """Append ``BREAK_GLASS_ACCESS`` when the caller is acting under a break-glass grant.

        §5.3 note 1 requires the grant **and every access during it** to be audited and visible
        in the target organization's log. Returns ``None`` for ordinary access so call sites can
        invoke it unconditionally.
        """
        if not context.via_break_glass:
            return None
        return self.record(
            AuditEvent(
                action=AuditAction.BREAK_GLASS_ACCESS,
                entity_type=entity_type,
                entity_id=entity_id,
                actor_user_id=context.user_id,
                actor_role_codes=format_actor_roles(context.roles),
                organization_id=context.organization_id,
                reason="Access under an active break-glass grant",
                request_id=context.metadata.request_id,
                ip_address=context.metadata.ip_address,
                user_agent=context.metadata.user_agent,
            )
        )

    def record_cross_org_denial(
        self,
        *,
        actor_user_id: uuid.UUID,
        actor_role_codes: str,
        organization_id: uuid.UUID | None,
        attempted_entity_type: str,
        attempted_entity_id: uuid.UUID,
        request_id: str | None = None,
        ip_address: str | None = None,
    ) -> AuditLog:
        """Append ``CROSS_ORG_ACCESS_DENIED`` for a refused cross-tenant attempt.

        AC-01 and AC-19 both require the *attempt* to be recorded, not merely refused — a 404
        with no audit row leaves no evidence (DEC-040).
        """
        return self.record(
            AuditEvent(
                action=AuditAction.CROSS_ORG_ACCESS_DENIED,
                entity_type=attempted_entity_type,
                entity_id=attempted_entity_id,
                actor_user_id=actor_user_id,
                actor_role_codes=actor_role_codes,
                organization_id=organization_id,
                request_id=request_id,
                ip_address=ip_address,
            )
        )

    # ---------------------------------------------------------------- integrity hashing

    @staticmethod
    def _record_hash(row: AuditLog, *, previous: str | None = None) -> str:
        """SHA-256 over this row's canonical content (§20.1 ``integrity_hash``).

        Detects in-place modification of a row. Canonicalised with sorted keys so the digest
        does not depend on dict ordering.
        """
        payload = {
            "previous": previous or "",
            "occurred_at": row.occurred_at.isoformat() if row.occurred_at else "",
            "organization_id": str(row.organization_id) if row.organization_id else "",
            "actor_user_id": str(row.actor_user_id),
            "action": row.action,
            "entity_type": row.entity_type,
            "entity_id": str(row.entity_id),
            "changed_fields": row.changed_fields or [],
            "before_values": row.before_values or {},
            "after_values": row.after_values or {},
            "reason": row.reason or "",
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _latest_hash(self, organization_id: uuid.UUID) -> str | None:
        """The most recent ``integrity_hash`` for an organization, for chaining."""
        statement = (
            select(AuditLog.integrity_hash)
            .where(AuditLog.organization_id == organization_id)
            .order_by(AuditLog.occurred_at.desc(), AuditLog.id.desc())
            .limit(1)
        )
        return self._session.execute(statement).scalar_one_or_none()


__all__ = ["AuditWriter"]
