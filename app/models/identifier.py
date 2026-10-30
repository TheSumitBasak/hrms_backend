"""Encrypted employee identifiers (§11.1).

Identifiers are **rows, not plaintext columns**. Each row holds AES-256-GCM ciphertext, an
HMAC blind index for exact-match search and duplicate detection, and the last four characters
for masked display. Changing a value inserts a new row and marks the old one historical, so
the identifier history of §20.4 is preserved without a separate history table.

Three columns do three different jobs and must not be confused:

``value_encrypted``
    The only place the real value exists. Opened only in the service layer for an explicit,
    audited reveal (§11.6), never for a mask and never for a duplicate check.
``value_blind_index``
    A keyed digest. All duplicate detection (§11.5) and identifier search (§26.2) run on this,
    so plaintext is never compared or decrypted for those purposes.
``value_last_four``
    Drives masked display with no decryption at all (§11.6).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Computed,
    Date,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    SmallInteger,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.masking import IdentifierType
from app.db.base import Base
from app.db.mixins import StandardColumns, archive_reason_constraint
from app.db.types import Uuid7, blind_index, ciphertext

MULTI_VALUE_IDENTIFIER_TYPES: frozenset[IdentifierType] = frozenset(
    {IdentifierType.PREVIOUS_PF_MEMBER_ID}
)
"""§11.1: exactly one current row per (employee, type), **except** previous PF member IDs,
of which an employee may legitimately have several at once — one per prior employer."""

BLIND_INDEX_LENGTH = 32
"""HMAC-SHA256 output, matching ``BYTES(32)`` in §11.1."""


class VerificationStatus(StrEnum):
    """§11.1 ``verification_status``.

    A manual flag only. §1.3 puts external verification out of scope, so nothing here contacts
    an issuing authority — passing a checksum is a format check, not proof of existence.
    """

    UNVERIFIED = "UNVERIFIED"
    VERIFIED = "VERIFIED"
    REJECTED = "REJECTED"


class EmployeeIdentifier(Base, StandardColumns):
    """One identifier value for one employee (§11.1)."""

    __tablename__ = "employee_identifiers"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_employee_identifiers_org_id"),
        # C-06 / §23.3: one current row per type, except PREVIOUS_PF_MEMBER_ID. The generated
        # key is NULL for historical rows and for the multi-valued type, and NULLs do not
        # collide — reproducing the PostgreSQL partial unique index (ADR-003).
        UniqueConstraint(
            "employee_id", "current_identifier_key", name="uq_employee_identifiers_current_type"
        ),
        ForeignKeyConstraint(
            ["organization_id", "employee_id"],
            ["employees.organization_id", "employees.id"],
            name="fk_employee_identifiers_employee",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "identifier_type IN ("
            + ",".join(f"'{member.value}'" for member in IdentifierType)
            + ")",
            name="ck_employee_identifiers_type",
        ),
        CheckConstraint(
            "verification_status IN ('UNVERIFIED','VERIFIED','REJECTED')",
            name="ck_employee_identifiers_verification_status",
        ),
        CheckConstraint(
            "expiry_date IS NULL OR issue_date IS NULL OR expiry_date > issue_date",
            name="ck_employee_identifiers_expiry_after_issue",
        ),
        # §11.1: a passport needs an issuing country and an expiry date.
        CheckConstraint(
            "identifier_type <> 'PASSPORT' "
            "OR (issuing_country IS NOT NULL AND expiry_date IS NOT NULL)",
            name="ck_employee_identifiers_passport_requires_country_and_expiry",
        ),
        CheckConstraint(
            f"LENGTH(value_blind_index) = {BLIND_INDEX_LENGTH}",
            name="ck_employee_identifiers_blind_index_length",
        ),
        CheckConstraint("key_version >= 1", name="ck_employee_identifiers_key_version"),
        CheckConstraint(
            "verification_status = 'UNVERIFIED' OR verified_at IS NOT NULL",
            name="ck_employee_identifiers_verified_at_present",
        ),
        # C-13 / §6.3: the referenced document must belong to the same employee.
        ForeignKeyConstraint(
            ["employee_id", "supporting_document_id"],
            ["employee_documents.employee_id", "employee_documents.id"],
            name="fk_employee_identifiers_supporting_document",
            ondelete="RESTRICT",
            use_alter=True,
        ),
        archive_reason_constraint("employee_identifiers"),
        # §23.3: duplicate detection within an organization, and across a group. The group
        # lookup omits organization_id because §11.5 matches across the organizations of one
        # group — which is safe only because the blind-index **key** is group-scoped, so a
        # digest from another group cannot match in the first place (§23.7).
        Index(
            "ix_employee_identifiers_org_type_blind",
            "organization_id",
            "identifier_type",
            "value_blind_index",
        ),
        Index("ix_employee_identifiers_type_blind", "identifier_type", "value_blind_index"),
        Index("ix_employee_identifiers_employee_current", "employee_id", "is_current"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    employee_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    identifier_type: Mapped[str] = mapped_column(String(30), nullable=False)

    value_encrypted: Mapped[bytes] = mapped_column(ciphertext(), nullable=False)
    """AES-256-GCM: 12-byte nonce, ciphertext, 16-byte tag. Additional authenticated data binds
    it to this organization, employee and identifier type, so the blob cannot be relocated to
    another row or tenant (§23.7). **Never logged, never audited, never serialised.**"""

    value_blind_index: Mapped[bytes] = mapped_column(blind_index(), nullable=False)
    value_last_four: Mapped[str] = mapped_column(String(4), nullable=False)
    key_version: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=1, server_default=text("1")
    )
    """Which key version sealed this row, so rotation can re-encrypt lazily (§23.7)."""

    issuing_country: Mapped[str | None] = mapped_column(String(2), nullable=True)
    issue_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    expiry_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    issue_place: Mapped[str | None] = mapped_column(String(100), nullable=True)

    is_current: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("1")
    )
    """Changing a value inserts a new row and clears this on the old one (§11.1), which is what
    keeps identifier history without overwriting."""

    current_identifier_key: Mapped[str | None] = mapped_column(
        String(30),
        Computed(
            "IF(is_current = 1 AND identifier_type <> 'PREVIOUS_PF_MEMBER_ID', "
            "identifier_type, NULL)",
            persisted=True,
        ),
        nullable=True,
    )

    verification_status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=VerificationStatus.UNVERIFIED,
        server_default="UNVERIFIED",
    )
    verified_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid7, ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    verified_at: Mapped[datetime | None] = mapped_column(nullable=True)

    supporting_document_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    """Composite FK to ``employee_documents`` on (employee_id, id) is added once that table
    exists; see :mod:`app.models.document`."""

    duplicate_override_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    """§11.1: required when the value was saved despite a blocking duplicate, which needs
    ``identity.duplicate_override`` and is audited as ``DUPLICATE_OVERRIDE``."""
