"""Statutory registrations and per-organization policy configuration (§7.4, §11.3, §13.3, §21.2).

All of these exist so that statutory applicability, identifier requirements and nomination
rules are **configuration rather than code** (NFR-15). The system never infers which
registrations or identifiers an organization needs — §11.3 is explicit: "The OA decides
requirements; the system does not infer statutory applicability."
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
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.masking import IdentifierType
from app.db.base import Base
from app.db.mixins import (
    PrimaryKeyColumn,
    StandardColumns,
    archive_reason_constraint,
    status_constraint,
)
from app.db.types import Uuid7

PURPOSE_STATEMENT_MIN_LENGTH = 30
"""§11.3: a purpose statement of at least thirty characters is required before Aadhaar, voter
ID or driving licence may be collected at all."""


class RegistrationType(StrEnum):
    """§7.4 ``registration_type``."""

    GSTIN = "GSTIN"
    TAN = "TAN"
    PF_ESTABLISHMENT = "PF_ESTABLISHMENT"
    ESIC_EMPLOYER = "ESIC_EMPLOYER"
    ESIC_SUB_CODE = "ESIC_SUB_CODE"
    PT_REGISTRATION = "PT_REGISTRATION"
    PT_ENROLMENT = "PT_ENROLMENT"
    LWF = "LWF"
    SHOPS_ESTABLISHMENTS = "SHOPS_ESTABLISHMENTS"
    FACTORY_LICENSE = "FACTORY_LICENSE"
    CONTRACT_LABOUR_LICENSE = "CONTRACT_LABOUR_LICENSE"
    OTHER = "OTHER"


SYSTEM_WIDE_UNIQUE_TYPES: frozenset[RegistrationType] = frozenset(
    {
        RegistrationType.GSTIN,
        RegistrationType.PF_ESTABLISHMENT,
        RegistrationType.ESIC_EMPLOYER,
    }
)
"""§7.4: these numbers identify one legal entity, so they are unique across all tenants."""

STATE_SCOPED_TYPES: frozenset[RegistrationType] = frozenset(
    {
        RegistrationType.GSTIN,
        RegistrationType.ESIC_SUB_CODE,
        RegistrationType.PT_REGISTRATION,
        RegistrationType.PT_ENROLMENT,
        RegistrationType.LWF,
    }
)
"""§7.4: ``state_code`` is mandatory for these."""

STATE_UNIQUE_TYPES: frozenset[RegistrationType] = frozenset(
    {
        RegistrationType.ESIC_SUB_CODE,
        RegistrationType.PT_REGISTRATION,
        RegistrationType.PT_ENROLMENT,
        RegistrationType.LWF,
    }
)
"""§7.4: unique within organization **and** state."""

BRANCH_DERIVED_TYPES: frozenset[RegistrationType] = frozenset(
    {
        RegistrationType.GSTIN,
        RegistrationType.SHOPS_ESTABLISHMENTS,
        RegistrationType.FACTORY_LICENSE,
    }
)
"""The three types §8.1 exposes as derived read-only fields on a branch (DEC-015)."""


def _quoted(members: frozenset[RegistrationType]) -> str:
    return ",".join(f"'{member.value}'" for member in sorted(members))


class OrganizationRegistration(Base, StandardColumns):
    """One statutory registration (§7.4).

    Registrations are rows, not columns, because an organization may hold several of the same
    type — multiple GSTINs across states, a TAN per branch — and because re-registration must
    be kept as history with non-overlapping effective intervals.

    **Uniqueness.** §7.4 gives each type a different scope: system-wide, per organization, per
    organization and state, or per organization and label. Rather than four conditional
    indexes, which MySQL cannot express, a single stored generated column encodes the scope
    into one key and one unique index enforces all four rules (ADR-003). The encoding includes
    the organization only for the scopes that are tenant-local, so a GSTIN really is rejected
    across tenants.
    """

    __tablename__ = "organization_registrations"
    __table_args__ = (
        UniqueConstraint("unique_scope_key", name="uq_organization_registrations_scope"),
        UniqueConstraint("organization_id", "id", name="uq_organization_registrations_org_id"),
        ForeignKeyConstraint(
            ["organization_id", "branch_id"],
            ["branches.organization_id", "branches.id"],
            name="fk_organization_registrations_branch",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            f"registration_type IN ({_quoted(frozenset(RegistrationType))})",
            name="ck_organization_registrations_type",
        ),
        # §7.4: state_code is mandatory for state-scoped types.
        CheckConstraint(
            f"registration_type NOT IN ({_quoted(STATE_SCOPED_TYPES)}) OR state_code IS NOT NULL",
            name="ck_organization_registrations_state_code_required",
        ),
        # §7.4: a label is mandatory for OTHER, since it is what distinguishes two of them.
        CheckConstraint(
            "registration_type <> 'OTHER' OR registration_label IS NOT NULL",
            name="ck_organization_registrations_other_requires_label",
        ),
        CheckConstraint(
            "effective_to IS NULL OR effective_from IS NULL OR effective_to > effective_from",
            name="ck_organization_registrations_effective_interval",
        ),
        status_constraint("organization_registrations"),
        archive_reason_constraint("organization_registrations"),
        Index(
            "ix_organization_registrations_lookup",
            "organization_id",
            "registration_type",
            "state_code",
        ),
        Index("ix_organization_registrations_branch", "organization_id", "branch_id"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid7, ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    branch_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    """Null means organization- or state-level rather than branch-level."""

    registration_type: Mapped[str] = mapped_column(String(40), nullable=False)
    registration_label: Mapped[str | None] = mapped_column(String(100), nullable=True)
    registration_number: Mapped[str] = mapped_column(String(50), nullable=False)
    """Normalised upper-case with spaces removed (§7.4)."""

    state_code: Mapped[str | None] = mapped_column(String(2), nullable=True)
    """For a GSTIN this must equal characters 1-2 of the number (§7.4), checked in the service
    because MySQL generated columns cannot raise a domain error."""

    issuing_authority: Mapped[str | None] = mapped_column(String(150), nullable=True)
    issue_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    effective_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    """Exclusive (§1.4). A registration displays as "Expired" once this date has passed."""

    status: Mapped[str] = mapped_column(String(10), nullable=False, default="ACTIVE")
    document_file_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid7, ForeignKey("organization_files.id", ondelete="SET NULL"), nullable=True
    )
    remarks: Mapped[str | None] = mapped_column(String(500), nullable=True)

    unique_scope_key: Mapped[str] = mapped_column(
        String(180),
        Computed(
            # System-wide types omit the organization, so the number collides across tenants.
            "CASE "
            f"WHEN registration_type IN ({_quoted(SYSTEM_WIDE_UNIQUE_TYPES)}) "
            "  THEN CONCAT(registration_type, ':', registration_number) "
            f"WHEN registration_type IN ({_quoted(STATE_UNIQUE_TYPES)}) "
            "  THEN CONCAT(HEX(organization_id), ':', registration_type, ':', "
            "              COALESCE(state_code, ''), ':', registration_number) "
            "WHEN registration_type = 'OTHER' "
            "  THEN CONCAT(HEX(organization_id), ':OTHER:', "
            "              COALESCE(registration_label, ''), ':', registration_number) "
            "ELSE CONCAT(HEX(organization_id), ':', registration_type, ':', registration_number) "
            "END",
            persisted=True,
        ),
        nullable=False,
    )


class IdentifierRequirement(StrEnum):
    """§11.3 ``requirement``."""

    NOT_COLLECTED = "NOT_COLLECTED"
    OPTIONAL = "OPTIONAL"
    REQUIRED_FOR_ACTIVATION = "REQUIRED_FOR_ACTIVATION"


PURPOSE_REQUIRED_IDENTIFIERS: frozenset[IdentifierType] = frozenset(
    {IdentifierType.AADHAAR, IdentifierType.VOTER_ID, IdentifierType.DRIVING_LICENSE}
)
"""§11.3: collecting any of these at all requires a documented lawful purpose."""

DEFAULT_IDENTIFIER_REQUIREMENTS: dict[IdentifierType, IdentifierRequirement] = {
    IdentifierType.PAN: IdentifierRequirement.OPTIONAL,
    IdentifierType.AADHAAR: IdentifierRequirement.NOT_COLLECTED,
    IdentifierType.UAN: IdentifierRequirement.OPTIONAL,
    IdentifierType.PF_MEMBER_ID: IdentifierRequirement.OPTIONAL,
    IdentifierType.PREVIOUS_PF_MEMBER_ID: IdentifierRequirement.OPTIONAL,
    IdentifierType.ESIC_IP: IdentifierRequirement.OPTIONAL,
    IdentifierType.PASSPORT: IdentifierRequirement.OPTIONAL,
    IdentifierType.VOTER_ID: IdentifierRequirement.NOT_COLLECTED,
    IdentifierType.DRIVING_LICENSE: IdentifierRequirement.NOT_COLLECTED,
}
"""§11.3 seeded defaults. Aadhaar, voter ID and driving licence are **not collected** until
an administrator enables them — the data-minimisation default of §25.2 and DEC-030."""


class IdentifierRequirementPolicy(Base, StandardColumns):
    """Whether and how strictly one identifier type is collected (§11.3)."""

    __tablename__ = "identifier_requirement_policies"
    __table_args__ = (
        UniqueConstraint(
            "organization_id", "identifier_type", name="uq_identifier_requirement_policies_org_type"
        ),
        UniqueConstraint("organization_id", "id", name="uq_identifier_requirement_policies_org_id"),
        CheckConstraint(
            "identifier_type IN ("
            + ",".join(f"'{member.value}'" for member in IdentifierType)
            + ")",
            name="ck_identifier_requirement_policies_type",
        ),
        CheckConstraint(
            "requirement IN ('NOT_COLLECTED','OPTIONAL','REQUIRED_FOR_ACTIVATION')",
            name="ck_identifier_requirement_policies_requirement",
        ),
        # §11.3: Aadhaar, voter ID and driving licence need a recorded purpose before they may
        # be collected. Enforced in the database so the flag and its justification cannot be
        # separated by any write path (§25.2).
        CheckConstraint(
            "requirement = 'NOT_COLLECTED' "
            "OR identifier_type NOT IN ('AADHAAR','VOTER_ID','DRIVING_LICENSE') "
            "OR (purpose_statement IS NOT NULL "
            f"AND CHAR_LENGTH(purpose_statement) >= {PURPOSE_STATEMENT_MIN_LENGTH})",
            name="ck_identifier_requirement_policies_purpose",
        ),
        archive_reason_constraint("identifier_requirement_policies"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid7, ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    identifier_type: Mapped[str] = mapped_column(String(30), nullable=False)
    requirement: Mapped[str] = mapped_column(String(30), nullable=False)
    purpose_statement: Mapped[str | None] = mapped_column(String(500), nullable=True)


class NominationType(StrEnum):
    """§13.2 ``nomination_type``. Shares are never mixed across types."""

    PF = "PF"
    GRATUITY = "GRATUITY"
    INSURANCE = "INSURANCE"
    OTHER = "OTHER"


DEFAULT_NOMINATION_CONFIGS: dict[NominationType, tuple[bool, bool]] = {
    # (requires_full_allocation, requires_dob) — §13.3 defaults
    NominationType.PF: (True, True),
    NominationType.GRATUITY: (True, True),
    NominationType.INSURANCE: (True, False),
    NominationType.OTHER: (False, False),
}


class NominationTypeConfig(Base, StandardColumns):
    """Per-organization nomination rules (§13.3).

    Only share totals, minor/guardian and date-of-birth rules are configurable. Scheme-specific
    statutory eligibility is deliberately **not** hardcoded (§29 Q-11, DEC-035);
    ``scheme_rule_notes`` is free text for whatever legal review concludes.
    """

    __tablename__ = "nomination_type_configs"
    __table_args__ = (
        UniqueConstraint(
            "organization_id", "nomination_type", name="uq_nomination_type_configs_org_type"
        ),
        UniqueConstraint("organization_id", "id", name="uq_nomination_type_configs_org_id"),
        CheckConstraint(
            "nomination_type IN ('PF','GRATUITY','INSURANCE','OTHER')",
            name="ck_nomination_type_configs_type",
        ),
        CheckConstraint(
            "max_nominees IS NULL OR max_nominees BETWEEN 1 AND 20",
            name="ck_nomination_type_configs_max_nominees",
        ),
        archive_reason_constraint("nomination_type_configs"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid7, ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    nomination_type: Mapped[str] = mapped_column(String(20), nullable=False)
    requires_full_allocation: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    """When true, active shares must total exactly 100.00 (C-08)."""

    requires_dob: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    max_nominees: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    """Null means unlimited."""

    scheme_rule_notes: Mapped[str | None] = mapped_column(String(1000), nullable=True)


class ChecksumAlgorithm(StrEnum):
    """§21.2 ``checksum_algorithm``."""

    NONE = "NONE"
    VERHOEFF = "VERHOEFF"
    GSTIN_MOD36 = "GSTIN_MOD36"


class NormalizationStrategy(StrEnum):
    """§21.2 ``normalization``."""

    UPPER_STRIP = "UPPER_STRIP"
    DIGITS_ONLY = "DIGITS_ONLY"
    UPPER_STRIP_SLASH = "UPPER_STRIP_SLASH"


class IdentifierTypeConfig(Base):
    """Platform-level, SSA-managed identifier validation configuration (§21.2).

    Lets an administrator adjust a pattern, severity or mask width without a deployment —
    §21.1 notes several Indian identifier formats have older variants still in circulation,
    and §7.3 says "the authoritative validation patterns are configurable by the SSA".

    ``identifier_type`` is the primary key and covers both employee and organization identifier
    types, so the key is a plain string rather than one of the narrower enums. §21.2 is
    explicit that changes "apply to new writes only": existing rows are never revalidated, so
    tightening a pattern cannot retroactively invalidate stored data.
    """

    __tablename__ = "identifier_type_configs"
    __table_args__ = (
        CheckConstraint(
            "normalization IN ('UPPER_STRIP','DIGITS_ONLY','UPPER_STRIP_SLASH')",
            name="ck_identifier_type_configs_normalization",
        ),
        CheckConstraint(
            "checksum_algorithm IN ('NONE','VERHOEFF','GSTIN_MOD36')",
            name="ck_identifier_type_configs_checksum",
        ),
        CheckConstraint(
            "severity IN ('ERROR','WARNING')", name="ck_identifier_type_configs_severity"
        ),
        CheckConstraint(
            "mask_visible_chars BETWEEN 0 AND 4", name="ck_identifier_type_configs_mask_chars"
        ),
    )

    identifier_type: Mapped[str] = mapped_column(String(30), primary_key=True)
    pattern: Mapped[str] = mapped_column(String(200), nullable=False)
    normalization: Mapped[str] = mapped_column(String(20), nullable=False)
    checksum_algorithm: Mapped[str] = mapped_column(
        String(20), nullable=False, default=ChecksumAlgorithm.NONE
    )
    severity: Mapped[str] = mapped_column(String(10), nullable=False, default="ERROR")
    mask_visible_chars: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=4, server_default=text("4")
    )
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid7, ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP(6)"),
        server_onupdate=text("CURRENT_TIMESTAMP(6)"),
    )


class OrganizationNoticeVersion(Base, PrimaryKeyColumn):
    """A published privacy-notice version (DEC-010, supporting §25.3).

    §25.3 requires ``employee_privacy_notices.notice_version`` to "match an organization-
    published notice version" and stores the notice text as an organization file. Without a
    registry of published versions that validation has nothing to check against.
    """

    __tablename__ = "organization_notice_versions"
    __table_args__ = (
        UniqueConstraint(
            "organization_id", "notice_version", name="uq_organization_notice_versions_org_version"
        ),
        UniqueConstraint("organization_id", "id", name="uq_organization_notice_versions_org_id"),
        ForeignKeyConstraint(
            ["organization_id", "notice_file_id"],
            ["organization_files.organization_id", "organization_files.id"],
            name="fk_organization_notice_versions_file",
            ondelete="RESTRICT",
        ),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid7, ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    notice_version: Mapped[str] = mapped_column(String(20), nullable=False)
    notice_file_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    published_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=text("CURRENT_TIMESTAMP(6)")
    )
    published_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid7, ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
