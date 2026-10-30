"""Document categories and employee document metadata (§15).

**Only metadata lives here.** File bytes are in private object storage; ``storage_key`` names
the object, contains no PII, and is never returned by the API (§15.3).

Versions are rows in ``employee_documents`` linked by ``supersedes_document_id`` rather than a
separate versions table — the §15.3 design decision. Every version is then independently
addressable, verifiable and auditable through one access-control path, which is the whole
point: a second table would mean a second place to get authorization wrong.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    SmallInteger,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.mixins import StandardColumns, archive_reason_constraint, status_constraint
from app.db.types import Uuid7, ciphertext

DEFAULT_MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024
"""NFR-18: 10 MB default, configurable 1-25 MB per category."""

MIN_CATEGORY_FILE_SIZE_BYTES = 1024 * 1024
MAX_CATEGORY_FILE_SIZE_BYTES = 25 * 1024 * 1024

PHOTOGRAPH_MAX_FILE_SIZE_BYTES = 2 * 1024 * 1024
"""NFR-18: photographs are capped at 2 MB."""

ALLOWED_MIME_TYPES: frozenset[str] = frozenset(
    {
        "application/pdf",
        "image/jpeg",
        "image/png",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }
)
"""§15.5 allowlist. The first three are the default; DOCX is optional per category. Anything
outside this set is rejected with ``415 FILE_TYPE_NOT_ALLOWED``, and so is any file whose
sniffed type disagrees with its extension."""


class UploadStatus(StrEnum):
    """§15.3 ``upload_status``. Only ``AVAILABLE`` is downloadable."""

    PENDING_UPLOAD = "PENDING_UPLOAD"
    SCANNING = "SCANNING"
    AVAILABLE = "AVAILABLE"
    QUARANTINED = "QUARANTINED"
    FAILED = "FAILED"


class DocumentVerificationStatus(StrEnum):
    """§15.3 ``verification_status``."""

    PENDING = "PENDING"
    VERIFIED = "VERIFIED"
    REJECTED = "REJECTED"
    NOT_REQUIRED = "NOT_REQUIRED"


class ExpiryState(StrEnum):
    """§15.8 ``expiry_state``, derived — never stored.

    Computed from ``expiry_date`` and the organization's warning window on every read, because
    a stored value would be wrong the moment the clock passed midnight and §15.8 explicitly
    generates no notifications or jobs to refresh it.
    """

    NO_EXPIRY = "NO_EXPIRY"
    VALID = "VALID"
    EXPIRING_SOON = "EXPIRING_SOON"
    EXPIRED = "EXPIRED"


class DocumentCategory(Base, StandardColumns):
    """A document category: configuration, not storage (§15.2).

    Twenty-five categories are seeded per organization. Organizations may add categories and
    rename or deactivate existing ones, but ``category_code`` is immutable because documents
    and the ``SALARY_*`` permission rules reference it.
    """

    __tablename__ = "document_categories"
    __table_args__ = (
        UniqueConstraint(
            "organization_id", "category_code", name="uq_document_categories_org_code"
        ),
        UniqueConstraint("organization_id", "id", name="uq_document_categories_org_id"),
        CheckConstraint(
            "category_code REGEXP '^[A-Z0-9_]{2,40}$'", name="ck_document_categories_code_pattern"
        ),
        CheckConstraint(
            f"max_file_size_bytes BETWEEN {MIN_CATEGORY_FILE_SIZE_BYTES} "
            f"AND {MAX_CATEGORY_FILE_SIZE_BYTES}",
            name="ck_document_categories_max_file_size",
        ),
        CheckConstraint(
            "retention_months_after_separation IS NULL "
            "OR retention_months_after_separation BETWEEN 0 AND 600",
            name="ck_document_categories_retention_months",
        ),
        status_constraint("document_categories"),
        archive_reason_constraint("document_categories"),
        Index("ix_document_categories_org_status", "organization_id", "status"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid7, ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    category_code: Mapped[str] = mapped_column(String(40), nullable=False)
    category_name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)

    is_restricted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    """Requires ``document.download_restricted`` to download (§15.6)."""

    requires_verification: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    supports_expiry: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    allows_multiple_current: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("1")
    )
    """When false, one current document per (employee, category, document_type) — C-09."""

    max_file_size_bytes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=DEFAULT_MAX_FILE_SIZE_BYTES
    )
    is_mandatory_for_activation: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    """§10.3: checked during the DRAFT → ACTIVE activation check."""

    retention_months_after_separation: Mapped[int | None] = mapped_column(
        SmallInteger, nullable=True
    )
    """§25.6. Null falls back to the organization default. A purge still requires explicit
    approval (DEC-031)."""

    status: Mapped[str] = mapped_column(String(10), nullable=False, default="ACTIVE")
    is_system_default: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )


class DocumentCategoryMimeType(Base):
    """An allowed MIME type for a category (§15.2).

    A child table, because §15.2 and §23.1 both forbid a comma-separated column. At least one
    row is required per category, which the service enforces — an empty allowlist would make a
    category silently unusable rather than visibly misconfigured.
    """

    __tablename__ = "document_category_mime_types"
    __table_args__ = (
        CheckConstraint(
            "mime_type IN (" + ",".join(f"'{value}'" for value in sorted(ALLOWED_MIME_TYPES)) + ")",
            name="ck_document_category_mime_types_allowlist",
        ),
    )

    document_category_id: Mapped[uuid.UUID] = mapped_column(
        Uuid7, ForeignKey("document_categories.id", ondelete="CASCADE"), primary_key=True
    )
    mime_type: Mapped[str] = mapped_column(String(100), primary_key=True)


class EmployeeDocument(Base, StandardColumns):
    """Metadata for one version of one employee document (§15.3)."""

    __tablename__ = "employee_documents"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_employee_documents_org_id"),
        # §6.3: lets child records reference a document and have the database confirm it
        # belongs to the same employee (C-13).
        UniqueConstraint("employee_id", "id", name="uq_employee_documents_employee_id"),
        # §23.3: one row may supersede at most one other, so a version chain stays linear.
        UniqueConstraint("supersedes_document_id", name="uq_employee_documents_supersedes"),
        # C-09: at most one current document per (employee, category, document_type) for
        # single-current categories. `single_current_key` is maintained by the service rather
        # than generated, because the condition depends on the *category's*
        # `allows_multiple_current` flag, which a generated column cannot reach (DEC-017).
        UniqueConstraint(
            "employee_id",
            "document_category_id",
            "document_type",
            "single_current_key",
            name="uq_employee_documents_single_current",
        ),
        ForeignKeyConstraint(
            ["organization_id", "employee_id"],
            ["employees.organization_id", "employees.id"],
            name="fk_employee_documents_employee",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "document_category_id"],
            ["document_categories.organization_id", "document_categories.id"],
            name="fk_employee_documents_category",
            ondelete="RESTRICT",
        ),
        # §22: a superseded version must stay in place, so RESTRICT rather than cascade.
        ForeignKeyConstraint(
            ["employee_id", "supersedes_document_id"],
            ["employee_documents.employee_id", "employee_documents.id"],
            name="fk_employee_documents_supersedes",
            ondelete="RESTRICT",
        ),
        CheckConstraint("file_size_bytes > 0", name="ck_employee_documents_size_positive"),
        # The three document-number columns are meaningful only together.
        CheckConstraint(
            "(document_number_encrypted IS NULL AND document_number_last_four IS NULL "
            " AND document_number_key_version IS NULL) "
            "OR (document_number_encrypted IS NOT NULL "
            " AND document_number_last_four IS NOT NULL "
            " AND document_number_key_version >= 1)",
            name="ck_employee_documents_document_number_triple",
        ),
        CheckConstraint("version_number >= 1", name="ck_employee_documents_version_number"),
        CheckConstraint(
            "expiry_date IS NULL OR issue_date IS NULL OR expiry_date > issue_date",
            name="ck_employee_documents_expiry_after_issue",
        ),
        CheckConstraint(
            "upload_status IN ('PENDING_UPLOAD','SCANNING','AVAILABLE','QUARANTINED','FAILED')",
            name="ck_employee_documents_upload_status",
        ),
        CheckConstraint(
            "verification_status IN ('PENDING','VERIFIED','REJECTED','NOT_REQUIRED')",
            name="ck_employee_documents_verification_status",
        ),
        # §15.3: a rejection must say why, in 10-500 characters.
        CheckConstraint(
            "verification_status <> 'REJECTED' OR ("
            "rejection_reason IS NOT NULL "
            "AND CHAR_LENGTH(rejection_reason) BETWEEN 10 AND 500)",
            name="ck_employee_documents_rejection_reason",
        ),
        CheckConstraint(
            "verification_status NOT IN ('VERIFIED','REJECTED') OR verified_at IS NOT NULL",
            name="ck_employee_documents_verified_at",
        ),
        archive_reason_constraint("employee_documents"),
        Index(
            "ix_employee_documents_employee_category_current",
            "organization_id",
            "employee_id",
            "document_category_id",
            "is_current",
        ),
        # §15.8: the organization-level "expiring documents" list.
        Index("ix_employee_documents_org_expiry", "organization_id", "expiry_date", "is_current"),
        # §15.3: duplicate content detection, driving the W-DOC-DUP warning.
        Index("ix_employee_documents_checksum", "employee_id", "checksum"),
        Index("ix_employee_documents_upload_status", "upload_status", "created_at"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    employee_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    document_category_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    document_type: Mapped[str] = mapped_column(String(50), nullable=False)
    """``LOOKUP(DOCUMENT_TYPE)`` sub-type; defaults to the category code (§15.3)."""

    document_title: Mapped[str] = mapped_column(String(200), nullable=False)
    document_description: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    original_file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    """Sanitised on upload: path separators and control characters removed (§15.3). Display
    only — it is never used to build a storage path, which is what makes path traversal
    structurally impossible rather than merely filtered."""

    storage_key: Mapped[str] = mapped_column(String(500), nullable=False)
    """``org/{org_uuid}/emp/{emp_uuid}/{doc_uuid}`` — contains no PII and is never returned by
    the API (§15.3)."""

    mime_type: Mapped[str] = mapped_column(String(100), nullable=False)
    """Detected by **content sniffing**, not taken from the client (§15.3, §15.5, E-22)."""

    file_extension: Mapped[str] = mapped_column(String(10), nullable=False)
    file_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    """SHA-256, computed server-side (§15.3)."""

    # §15.3 ``document_number`` is [E][R]: a certificate or ID number, encrypted at rest and
    # shown masked to its last four characters. Same three-column pattern as identifiers.
    document_number_encrypted: Mapped[bytes | None] = mapped_column(ciphertext(), nullable=True)
    document_number_last_four: Mapped[str | None] = mapped_column(String(4), nullable=True)
    document_number_key_version: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)

    issue_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    expiry_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    """Allowed only when the category supports expiry (§15.3), checked in the service because
    the rule depends on the category row."""

    issued_by: Mapped[str | None] = mapped_column(String(200), nullable=True)

    verification_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=DocumentVerificationStatus.PENDING
    )
    verified_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid7, ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    verified_at: Mapped[datetime | None] = mapped_column(nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)

    upload_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=UploadStatus.PENDING_UPLOAD
    )
    version_number: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    supersedes_document_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    is_current: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("1")
    )
    single_current_key: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    """1 when this row is the single current document for a single-current category, otherwise
    NULL. Maintained by the service; see the unique constraint above and DEC-017."""

    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid7, ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    uploaded_at: Mapped[datetime | None] = mapped_column(nullable=True)
    scan_result: Mapped[str | None] = mapped_column(String(200), nullable=True)
    """Why a document was quarantined or failed, for operational diagnosis (§15.4)."""
