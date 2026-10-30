"""Append-only audit log (§20.1) and the integrity-checker findings table (DEC-008).

``audit_logs`` is append-only in three reinforcing ways:

1. It uses :class:`app.db.mixins.AppendOnlyColumns`, so it has no ``updated_*``,
   ``row_version`` or ``archived_*`` columns — there is no ORM machinery for updating it.
2. The repository exposes insert and select only; there is no update or delete path.
3. §20.1 requires the **database grant** itself to be INSERT and SELECT only
   (``REVOKE UPDATE, DELETE ON ems.audit_logs``), which is a deployment step documented in
   ``docs/DEPLOYMENT.md``. That is the layer that holds even against a bug in this codebase.

``organization_id`` is nullable, and only for platform events (§20.1): organization creation,
role definition changes and the platform user lifecycle have no single tenant.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, CheckConstraint, ForeignKey, Index, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.mixins import AppendOnlyColumns, PrimaryKeyColumn
from app.db.types import Uuid7


class AuditLog(Base, AppendOnlyColumns):
    """One audited event (§20.1).

    Written in the **same transaction** as the change it describes (NFR-10, AC-21), so a
    committed business change always has its audit row and a rolled-back one leaves none.

    The payload columns carry redacted values only: identifier changes appear as a masked
    before/after, other sensitive values as ``"***changed***"``, and secrets, keys, ciphertext
    and file contents never appear at all (§20.3). That reduction happens in
    :func:`app.audit.events.build_change_set`, and the writer re-checks it before insert —
    because this table cannot be corrected after the fact.
    """

    __tablename__ = "audit_logs"
    __table_args__ = (
        CheckConstraint("CHAR_LENGTH(action) >= 3", name="ck_audit_logs_action_present"),
        # §20.1 indexes, all tenant-leading because every audit read is organization-scoped
        # (§24.4.8), plus the actor index for the §25.5 access review.
        Index("ix_audit_logs_org_occurred", "organization_id", "occurred_at"),
        Index("ix_audit_logs_org_entity", "organization_id", "entity_type", "entity_id"),
        Index(
            "ix_audit_logs_org_employee_occurred", "organization_id", "employee_id", "occurred_at"
        ),
        Index("ix_audit_logs_actor_occurred", "actor_user_id", "occurred_at"),
        Index("ix_audit_logs_org_action_occurred", "organization_id", "action", "occurred_at"),
    )

    occurred_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=text("CURRENT_TIMESTAMP(6)")
    )
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid7, ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=True
    )
    """Null only for platform events (§20.1)."""

    actor_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid7, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    actor_role_codes: Mapped[str] = mapped_column(String(100), nullable=False)
    """Roles held at the time of the action, sorted — a historical snapshot, so a later role
    change does not rewrite what the actor was permitted to do then (§20.1 ``[H]``)."""

    action: Mapped[str] = mapped_column(String(60), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(60), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    """Not a foreign key: the audit trail must outlive hard deletions of never-referenced
    configuration rows and retention purges, and a FK would either block the purge or cascade
    the evidence away with it."""

    employee_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    """Denormalised so the per-employee audit view of §24.4.8 is a single indexed read."""

    changed_fields: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True)
    before_values: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    after_values: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    reason: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    """§20.1: mandatory for backdated changes, corrections, reveals, overrides, break-glass and
    code reassignment. Enforced by :class:`app.audit.events.AuditEvent`."""

    acknowledged_warnings: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True)
    """§21.3: "acknowledged warnings are stored in the audit record"."""

    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)
    """Personal data; subject to the §25.6 retention schedule like the rest of the row."""

    user_agent: Mapped[str | None] = mapped_column(String(300), nullable=True)

    integrity_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    """§20.1 (optional): SHA-256 over the previous hash plus this record, per organization, so
    a deletion or reordering in the chain is detectable even though the table is append-only."""

    previous_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    """The link the hash chain is computed over. Stored so verification needs no ordering
    assumptions beyond the chain itself."""


class IntegrityFinding(Base, PrimaryKeyColumn):
    """A problem reported by the nightly integrity checker (DEC-008).

    MySQL cannot express the §23.5 exclusion constraints, so §18.8 requires "a nightly
    integrity job that reports any overlap as a P1 defect", and NFR-05 requires "zero orphan
    rows in nightly integrity check". Reporting needs somewhere durable and queryable to
    report *to*; this is it.

    Findings are resolved by fixing the data, so the row carries a resolution rather than being
    deleted — a recurring finding is itself diagnostic.
    """

    __tablename__ = "integrity_findings"
    __table_args__ = (
        CheckConstraint("severity IN ('P1','P2','P3')", name="ck_integrity_findings_severity"),
        CheckConstraint(
            "resolved_at IS NULL OR resolved_at >= detected_at",
            name="ck_integrity_findings_resolved_after_detected",
        ),
        Index("ix_integrity_findings_unresolved", "resolved_at", "severity"),
        Index("ix_integrity_findings_org_check", "organization_id", "check_name"),
    )

    detected_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=text("CURRENT_TIMESTAMP(6)")
    )
    check_name: Mapped[str] = mapped_column(String(80), nullable=False)
    """For example ``salary_assignment_overlap`` or ``cross_organization_mismatch``."""

    severity: Mapped[str] = mapped_column(String(2), nullable=False, default="P1")
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid7, ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=True
    )
    entity_type: Mapped[str] = mapped_column(String(60), nullable=False)
    entity_ids: Mapped[list[Any]] = mapped_column(JSON, nullable=False)
    """The rows involved — an overlap finding names both assignments."""

    detail: Mapped[str] = mapped_column(Text, nullable=False)
    """A description containing identifiers and dates only, never personal data (§25.9)."""

    resolved_at: Mapped[datetime | None] = mapped_column(nullable=True)
    resolution_note: Mapped[str | None] = mapped_column(String(1000), nullable=True)
