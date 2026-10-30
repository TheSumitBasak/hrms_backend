"""Privacy records: notices and consent, self-service change requests, erasure requests (§25).

§25.1 is careful about what these tables mean: implementing these controls does not by itself
establish legal compliance, and the lawful basis per data category must be confirmed by
counsel (§25.8 item 1). The system therefore *records* a basis chosen by the organization and
never infers one (DEC-028).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.mixins import PrimaryKeyColumn, StandardColumns, archive_reason_constraint
from app.db.types import Uuid7

SELF_EDITABLE_FIELDS: frozenset[str] = frozenset(
    {
        "personal_mobile",
        "alternate_mobile",
        "personal_email",
        "current_address",
        "emergency_contact",
    }
)
"""§5.6: the **only** fields an employee may submit a change request for. Anything else is
rejected — "No other self-service functionality is in scope"."""


class ProcessingBasis(StrEnum):
    """§25.3 ``basis``.

    Defaults to ``NOTICE_ONLY``. §25.1 notes the DPDP Act provides for certain legitimate uses
    including employment-related purposes, so consent is not assumed to be the only basis, and
    the system does not pick one on the organization's behalf.
    """

    NOTICE_ONLY = "NOTICE_ONLY"
    CONSENT = "CONSENT"


class NoticeMethod(StrEnum):
    """§25.3 ``method``."""

    ELECTRONIC = "ELECTRONIC"
    PAPER_UPLOADED = "PAPER_UPLOADED"


class ChangeRequestStatus(StrEnum):
    """Lifecycle of an employee self-service change request (§5.6, DEC-009)."""

    PENDING = "PENDING"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    WITHDRAWN = "WITHDRAWN"


class ErasureRequestStatus(StrEnum):
    """§25.7 erasure request lifecycle."""

    REQUESTED = "REQUESTED"
    APPROVED = "APPROVED"
    REFUSED = "REFUSED"
    COMPLETED = "COMPLETED"


class EmployeePrivacyNotice(Base, StandardColumns):
    """A notice delivery or consent record (§25.3).

    Withdrawing consent triggers a review of consent-based fields; it does **not**
    automatically delete records the organization is required by law to keep (§25.3), which is
    why withdrawal is a timestamp here and not a cascade.
    """

    __tablename__ = "employee_privacy_notices"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_employee_privacy_notices_org_id"),
        ForeignKeyConstraint(
            ["organization_id", "employee_id"],
            ["employees.organization_id", "employees.id"],
            name="fk_employee_privacy_notices_employee",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "basis IN ('NOTICE_ONLY','CONSENT')", name="ck_employee_privacy_notices_basis"
        ),
        CheckConstraint(
            "method IN ('ELECTRONIC','PAPER_UPLOADED')", name="ck_employee_privacy_notices_method"
        ),
        # §25.3: a consent record needs its scope, and only consent can be withdrawn.
        CheckConstraint(
            "basis <> 'CONSENT' OR consent_scope IS NOT NULL",
            name="ck_employee_privacy_notices_consent_scope",
        ),
        CheckConstraint(
            "withdrawn_at IS NULL OR basis = 'CONSENT'",
            name="ck_employee_privacy_notices_withdrawal_requires_consent",
        ),
        CheckConstraint(
            "acknowledged_at IS NULL OR acknowledged_at >= delivered_at",
            name="ck_employee_privacy_notices_acknowledged_after_delivered",
        ),
        # C-13 / §6.3: the referenced document must belong to the same employee.
        ForeignKeyConstraint(
            ["employee_id", "document_id"],
            ["employee_documents.employee_id", "employee_documents.id"],
            name="fk_employee_privacy_notices_document",
            ondelete="RESTRICT",
            use_alter=True,
        ),
        archive_reason_constraint("employee_privacy_notices"),
        Index("ix_employee_privacy_notices_employee", "employee_id", "delivered_at"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    employee_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    notice_version: Mapped[str] = mapped_column(String(20), nullable=False)
    """Must match a published version in ``organization_notice_versions`` (DEC-010)."""

    basis: Mapped[str] = mapped_column(
        String(20), nullable=False, default=ProcessingBasis.NOTICE_ONLY
    )
    consent_scope: Mapped[str | None] = mapped_column(String(200), nullable=True)
    delivered_at: Mapped[datetime] = mapped_column(nullable=False)
    acknowledged_at: Mapped[datetime | None] = mapped_column(nullable=True)
    withdrawn_at: Mapped[datetime | None] = mapped_column(nullable=True)
    method: Mapped[str] = mapped_column(String(20), nullable=False, default=NoticeMethod.ELECTRONIC)
    document_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    """For ``PAPER_UPLOADED``, the scanned acknowledgement (§25.3)."""


class EmployeeChangeRequest(Base, StandardColumns):
    """A pending self-service change submitted by an employee (§5.6, DEC-009).

    §5.6 requires changes to be "stored as pending and applied only after HRA/HRE acceptance
    (audited)". The requested values are held here rather than written to the employee record,
    so an unaccepted request never affects the system of record.

    The feature is off by default (``enable_employee_self_view = false``), and only the five
    fields in :data:`SELF_EDITABLE_FIELDS` may be requested.
    """

    __tablename__ = "employee_change_requests"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_employee_change_requests_org_id"),
        ForeignKeyConstraint(
            ["organization_id", "employee_id"],
            ["employees.organization_id", "employees.id"],
            name="fk_employee_change_requests_employee",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "status IN ('PENDING','ACCEPTED','REJECTED','WITHDRAWN')",
            name="ck_employee_change_requests_status",
        ),
        CheckConstraint(
            "status = 'PENDING' OR decided_at IS NOT NULL",
            name="ck_employee_change_requests_decided_at",
        ),
        CheckConstraint(
            "status <> 'REJECTED' OR decision_reason IS NOT NULL",
            name="ck_employee_change_requests_rejection_reason",
        ),
        archive_reason_constraint("employee_change_requests"),
        Index("ix_employee_change_requests_pending", "organization_id", "status", "created_at"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    employee_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    requested_by: Mapped[uuid.UUID] = mapped_column(
        Uuid7, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    requested_changes: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    """Only keys in :data:`SELF_EDITABLE_FIELDS`, validated on submission. Schema-validated
    JSON is permitted here by §23.1's list of allowed JSON uses by analogy with audit payloads;
    the alternative — a column per requestable field — would need a migration every time §5.6
    changed."""

    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=ChangeRequestStatus.PENDING
    )
    decided_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid7, ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    decided_at: Mapped[datetime | None] = mapped_column(nullable=True)
    decision_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)


class EmployeeErasureRequest(Base, StandardColumns):
    """A data-principal erasure request (§25.7).

    Approval is a human decision reviewed against retention obligations and the ``legal_hold``
    flag; §25.6 adds that a purge runs only after an OA approves the batch, with a two-person
    rule alongside an HRA. Nothing here deletes anything on its own — this table records the
    request and its outcome, and a refusal stores its reason.
    """

    __tablename__ = "employee_erasure_requests"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_employee_erasure_requests_org_id"),
        ForeignKeyConstraint(
            ["organization_id", "employee_id"],
            ["employees.organization_id", "employees.id"],
            name="fk_employee_erasure_requests_employee",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "status IN ('REQUESTED','APPROVED','REFUSED','COMPLETED')",
            name="ck_employee_erasure_requests_status",
        ),
        CheckConstraint(
            "status <> 'REFUSED' OR refusal_reason IS NOT NULL",
            name="ck_employee_erasure_requests_refusal_reason",
        ),
        CheckConstraint(
            "status <> 'COMPLETED' OR completed_at IS NOT NULL",
            name="ck_employee_erasure_requests_completed_at",
        ),
        archive_reason_constraint("employee_erasure_requests"),
        Index("ix_employee_erasure_requests_status", "organization_id", "status"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    employee_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    requested_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=text("CURRENT_TIMESTAMP(6)")
    )
    requester_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=ErasureRequestStatus.REQUESTED
    )
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid7, ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    refusal_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    anonymized_fields: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True)
    """Which fields were anonymised, so the §25.6 tombstone states what was removed."""


class RetentionPurgeBatch(Base, PrimaryKeyColumn):
    """An approved retention purge batch (§25.6).

    §25.6: "Retention purges run only after an OA approves the purge batch listing affected
    records (two-person rule with HRA)". The daily job therefore executes an approved batch and
    never decides for itself what to delete — which is what stops a misconfigured retention
    setting from quietly destroying records.
    """

    __tablename__ = "retention_purge_batches"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_retention_purge_batches_org_id"),
        CheckConstraint(
            "status IN ('PROPOSED','APPROVED','EXECUTED','CANCELLED')",
            name="ck_retention_purge_batches_status",
        ),
        # §25.6 two-person rule: the second approver must differ from the first.
        CheckConstraint(
            "second_approver_id IS NULL OR second_approver_id <> approved_by",
            name="ck_retention_purge_batches_two_person_rule",
        ),
        CheckConstraint(
            "status <> 'APPROVED' OR (approved_by IS NOT NULL AND second_approver_id IS NOT NULL)",
            name="ck_retention_purge_batches_approval_required",
        ),
        Index("ix_retention_purge_batches_status", "organization_id", "status"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid7, ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    proposed_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=text("CURRENT_TIMESTAMP(6)")
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="PROPOSED")
    scope_description: Mapped[str] = mapped_column(Text, nullable=False)
    affected_record_count: Mapped[int] = mapped_column(nullable=False, default=0)
    affected_entity_ids: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True)
    approved_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid7, ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    second_approver_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid7, ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(nullable=True)
    executed_at: Mapped[datetime | None] = mapped_column(nullable=True)
