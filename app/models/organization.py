"""Organizations, organization groups, settings and organization-level files (§7).

``organizations`` is the tenant root: every organization-scoped table carries
``organization_id`` pointing here, and the composite foreign keys of §6.3 all resolve through
it.

The §7.1 address group is written out column by column rather than generated from a mixin
factory. Generating it would save about thirty lines and cost the typed ``Mapped``
annotations, mypy coverage and the ability to read a table's real shape at its definition —
a poor trade for a group that appears four times with different prefixes.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, time
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    Date,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    Text,
    Time,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.clock import DEFAULT_FINANCIAL_YEAR_START_MONTH, DEFAULT_TIMEZONE
from app.db.base import Base
from app.db.mixins import (
    PrimaryKeyColumn,
    StandardColumns,
    archive_reason_constraint,
    status_constraint,
)
from app.db.types import Uuid7

CODE_PATTERN_SQL = "'^[A-Z][A-Z0-9]{1,9}$'"
"""§7.2/§7.6: ``^[A-Z][A-Z0-9]{1,9}$`` for organization and group codes."""


class EntityType(StrEnum):
    """§7.2 ``entity_type``. Drives the §7.3 identifier applicability rules."""

    PRIVATE_LIMITED = "PRIVATE_LIMITED"
    PUBLIC_LIMITED = "PUBLIC_LIMITED"
    OPC = "OPC"
    LLP = "LLP"
    PARTNERSHIP = "PARTNERSHIP"
    PROPRIETORSHIP = "PROPRIETORSHIP"
    TRUST = "TRUST"
    SOCIETY = "SOCIETY"
    SECTION_8_COMPANY = "SECTION_8_COMPANY"
    OTHER = "OTHER"


CIN_REQUIRED_TYPES: frozenset[EntityType] = frozenset(
    {
        EntityType.PRIVATE_LIMITED,
        EntityType.PUBLIC_LIMITED,
        EntityType.OPC,
        EntityType.SECTION_8_COMPANY,
    }
)
"""§7.3: these four require a CIN and must not carry an LLPIN."""

NO_REGISTRY_IDENTIFIER_TYPES: frozenset[EntityType] = frozenset(
    {
        EntityType.PARTNERSHIP,
        EntityType.PROPRIETORSHIP,
        EntityType.TRUST,
        EntityType.SOCIETY,
        EntityType.OTHER,
    }
)
"""§7.3: these must carry neither a CIN nor an LLPIN."""

EXPECTED_PAN_FOURTH_CHARACTER: dict[EntityType, str | None] = {
    EntityType.PRIVATE_LIMITED: "C",
    EntityType.PUBLIC_LIMITED: "C",
    EntityType.OPC: "C",
    EntityType.SECTION_8_COMPANY: "C",
    EntityType.LLP: "F",
    EntityType.PARTNERSHIP: "F",
    EntityType.PROPRIETORSHIP: "P",
    EntityType.TRUST: "T",
    # §7.3: "No check (configurable)" for SOCIETY, "No check" for OTHER.
    EntityType.SOCIETY: None,
    EntityType.OTHER: None,
}
"""§7.3 expected PAN holder-type character. A mismatch is a **warning** (`W-PAN-TYPE`), never
an error: government companies and subsidiaries of foreign companies legitimately differ."""

EXPECTED_CIN_OWNERSHIP_SEGMENT: dict[EntityType, str | None] = {
    EntityType.PRIVATE_LIMITED: "PTC",
    EntityType.PUBLIC_LIMITED: "PLC",
    EntityType.OPC: "OPC",
    EntityType.SECTION_8_COMPANY: "NPL",
}
"""§7.3 expected CIN characters 13-15. A mismatch is `W-CIN-TYPE`, a warning."""


def _entity_type_values() -> str:
    return ",".join(f"'{member.value}'" for member in EntityType)


class OrganizationGroup(Base, StandardColumns):
    """An optional grouping of organizations (§7.6).

    Groups hold no data of their own. They enable three things and nothing else: multi-
    organization grants created by an SSA, group-scoped duplicate identifier detection
    (§11.5), and the ``GROUP`` official-email uniqueness scope (§10.5).

    The group is also the **blind-index key scope** (§23.7): organizations in different groups
    derive different HMAC keys, so their identifier digests cannot match even in principle.
    """

    __tablename__ = "organization_groups"
    __table_args__ = (
        UniqueConstraint("group_code", name="uq_organization_groups_group_code"),
        CheckConstraint(
            f"group_code REGEXP {CODE_PATTERN_SQL}", name="ck_organization_groups_code"
        ),
        CheckConstraint(
            "CHAR_LENGTH(group_name) BETWEEN 2 AND 150", name="ck_organization_groups_name_length"
        ),
        status_constraint("organization_groups"),
        archive_reason_constraint("organization_groups"),
    )

    group_code: Mapped[str] = mapped_column(String(10), nullable=False)
    group_name: Mapped[str] = mapped_column(String(150), nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="ACTIVE")


class Organization(Base, StandardColumns):
    """A legally distinct entity and the tenant boundary (§7.2).

    Never hard-deleted once any employee, user grant or registration exists (§7.7); set
    ``status`` to ``INACTIVE`` instead.
    """

    __tablename__ = "organizations"
    __table_args__ = (
        # §23.1: case-insensitive uniqueness via a stored generated column, since MySQL has no
        # functional indexes of the PostgreSQL `upper(code)` form.
        UniqueConstraint("organization_code_normalized", name="uq_organizations_code_normalized"),
        UniqueConstraint("cin", name="uq_organizations_cin"),
        UniqueConstraint("llpin", name="uq_organizations_llpin"),
        # §23.3: PAN is unique system-wide *except* for proprietorships, where the PAN is the
        # proprietor's own and may legitimately recur. The generated column is NULL for a
        # proprietorship, and NULLs do not collide in a unique index — which is exactly the
        # semantics of a PostgreSQL partial unique index (ADR-003).
        UniqueConstraint("pan_unique_key", name="uq_organizations_pan_unique_key"),
        CheckConstraint(
            f"organization_code REGEXP {CODE_PATTERN_SQL}", name="ck_organizations_code"
        ),
        CheckConstraint(
            f"entity_type IN ({_entity_type_values()})", name="ck_organizations_entity_type"
        ),
        # §7.3 identifier applicability, enforced in the database rather than only in the
        # service: a CIN on a trust is a data-integrity error by any path.
        CheckConstraint(
            "("
            "entity_type IN ('PRIVATE_LIMITED','PUBLIC_LIMITED','OPC','SECTION_8_COMPANY') "
            "AND cin IS NOT NULL AND llpin IS NULL"
            ") OR ("
            "entity_type = 'LLP' AND llpin IS NOT NULL AND cin IS NULL"
            ") OR ("
            "entity_type IN ('PARTNERSHIP','PROPRIETORSHIP','TRUST','SOCIETY','OTHER') "
            "AND cin IS NULL AND llpin IS NULL"
            ")",
            name="ck_organizations_registry_identifier_applicability",
        ),
        CheckConstraint(
            "financial_year_start_month BETWEEN 1 AND 12",
            name="ck_organizations_financial_year_start_month",
        ),
        CheckConstraint(
            "date_of_incorporation IS NULL OR date_of_incorporation >= '1850-01-01'",
            name="ck_organizations_incorporation_not_before_1850",
        ),
        CheckConstraint(
            "CHAR_LENGTH(legal_name) BETWEEN 2 AND 200", name="ck_organizations_legal_name_length"
        ),
        CheckConstraint(
            "CHAR_LENGTH(display_name) BETWEEN 2 AND 100",
            name="ck_organizations_display_name_length",
        ),
        status_constraint("organizations"),
        archive_reason_constraint("organizations"),
        Index("ix_organizations_status", "status"),
        Index("ix_organizations_group_id", "group_id"),
    )

    # ---------------------------------------------------------------- identity
    organization_code: Mapped[str] = mapped_column(String(10), nullable=False)
    """Used by the ``{ORG}`` token of an employee code. §7.7 makes it immutable once any
    employee code has been issued, because issued codes embed it."""

    organization_code_normalized: Mapped[str] = mapped_column(
        String(10), Computed("UPPER(organization_code)", persisted=True), nullable=False
    )
    group_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid7, ForeignKey("organization_groups.id", ondelete="RESTRICT"), nullable=True
    )
    legal_name: Mapped[str] = mapped_column(String(200), nullable=False)
    display_name: Mapped[str] = mapped_column(String(100), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(40), nullable=False)
    industry: Mapped[str | None] = mapped_column(String(50), nullable=True)
    """``LOOKUP(INDUSTRY)`` — a ``value_code`` from this organization's own lookup list."""

    date_of_incorporation: Mapped[date | None] = mapped_column(Date, nullable=True)
    logo_document_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid7,
        ForeignKey("organization_files.id", ondelete="SET NULL", use_alter=True),
        nullable=True,
    )
    """PNG or JPEG only, at most 1 MB (DEC-013 resolves the §7.2 contradiction)."""

    website: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="ACTIVE")

    # ---------------------------------------------------------------- statutory identifiers
    cin: Mapped[str | None] = mapped_column(String(21), nullable=True)
    llpin: Mapped[str | None] = mapped_column(String(8), nullable=True)
    pan: Mapped[str | None] = mapped_column(String(10), nullable=True)
    pan_unique_key: Mapped[str | None] = mapped_column(
        String(10),
        Computed("IF(entity_type = 'PROPRIETORSHIP', NULL, pan)", persisted=True),
        nullable=True,
    )
    tan: Mapped[str | None] = mapped_column(String(10), nullable=True)
    """The primary TAN. Additional TANs are rows in ``organization_registrations``."""

    udyam_registration_number: Mapped[str | None] = mapped_column(String(19), nullable=True)

    # ---------------------------------------------------------------- registered address (§7.1)
    registered_address_line_1: Mapped[str] = mapped_column(String(200), nullable=False)
    registered_address_line_2: Mapped[str | None] = mapped_column(String(200), nullable=True)
    registered_address_city: Mapped[str] = mapped_column(String(100), nullable=False)
    registered_address_district: Mapped[str | None] = mapped_column(String(100), nullable=True)
    registered_address_state: Mapped[str] = mapped_column(String(100), nullable=False)
    registered_address_state_code: Mapped[str | None] = mapped_column(String(2), nullable=True)
    """Derived from the state via the ``states`` reference when the country is India (§7.1)."""

    registered_address_country: Mapped[str] = mapped_column(String(2), nullable=False, default="IN")
    registered_address_postal_code: Mapped[str] = mapped_column(String(10), nullable=False)

    # ---------------------------------------------------------------- corporate address (§7.1)
    corporate_address_line_1: Mapped[str | None] = mapped_column(String(200), nullable=True)
    corporate_address_line_2: Mapped[str | None] = mapped_column(String(200), nullable=True)
    corporate_address_city: Mapped[str | None] = mapped_column(String(100), nullable=True)
    corporate_address_district: Mapped[str | None] = mapped_column(String(100), nullable=True)
    corporate_address_state: Mapped[str | None] = mapped_column(String(100), nullable=True)
    corporate_address_state_code: Mapped[str | None] = mapped_column(String(2), nullable=True)
    corporate_address_country: Mapped[str | None] = mapped_column(String(2), nullable=True)
    corporate_address_postal_code: Mapped[str | None] = mapped_column(String(10), nullable=True)

    # ---------------------------------------------------------------- contacts
    official_phone: Mapped[str | None] = mapped_column(String(16), nullable=True)
    official_email: Mapped[str | None] = mapped_column(String(254), nullable=True)
    hr_contact_name: Mapped[str | None] = mapped_column(String(150), nullable=True)
    hr_contact_email: Mapped[str | None] = mapped_column(String(254), nullable=True)
    hr_contact_phone: Mapped[str | None] = mapped_column(String(16), nullable=True)

    # ---------------------------------------------------------------- locale and defaults
    financial_year_start_month: Mapped[int] = mapped_column(
        SmallInteger,
        nullable=False,
        default=DEFAULT_FINANCIAL_YEAR_START_MONTH,
        server_default=text(str(DEFAULT_FINANCIAL_YEAR_START_MONTH)),
    )
    """Used by the ``{YEAR}`` financial-year token formats (§19.1)."""

    default_currency: Mapped[str] = mapped_column(String(3), nullable=False, default="INR")
    timezone: Mapped[str] = mapped_column(
        String(50), nullable=False, default=DEFAULT_TIMEZONE, server_default=DEFAULT_TIMEZONE
    )
    """Defines "today" for this organization (§1.4). Changing it after codes are issued
    requires an acknowledged warning (§7.7)."""

    employee_id_format_settings: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    """§7.2: pre-fill defaults for **new** ID rules only. Never used for generation itself —
    generation reads the rule (§19.6). Unknown keys are rejected with 422."""

    @property
    def is_active(self) -> bool:
        return self.status == "ACTIVE" and self.archived_at is None


class OrganizationSettings(Base):
    """Per-organization configuration, 1:1 with the organization (§7.5).

    ``organization_id`` is both the primary key and the foreign key, which makes the
    one-to-one relationship a schema guarantee rather than a convention.

    These settings change validation behaviour, so they are read on nearly every write.
    Defaults match §7.5 exactly, and the privacy-relevant ones default to the most restrictive
    option the specification allows (§25.2): Aadhaar, religion, social category and disability
    are not collected until an administrator enables them with a recorded purpose.
    """

    __tablename__ = "organization_settings"
    __table_args__ = (
        CheckConstraint(
            "employee_code_assignment_point IN ('ON_CREATE','ON_ACTIVATION')",
            name="ck_organization_settings_code_assignment_point",
        ),
        CheckConstraint(
            "employee_id_date_basis IN ('JOINING_DATE','CREATION_DATE')",
            name="ck_organization_settings_id_date_basis",
        ),
        CheckConstraint(
            "official_email_uniqueness_scope IN ('ORGANIZATION','GROUP','NONE')",
            name="ck_organization_settings_email_scope",
        ),
        CheckConstraint(
            "date_display_format IN ('DD-MM-YYYY','YYYY-MM-DD')",
            name="ck_organization_settings_date_format",
        ),
        CheckConstraint(
            "minimum_employment_age_years BETWEEN 14 AND 21",
            name="ck_organization_settings_minimum_age",
        ),
        CheckConstraint(
            "leave_year_start_month BETWEEN 1 AND 12",
            name="ck_organization_settings_leave_year_month",
        ),
        CheckConstraint(
            "onboarding_reopen_window_days BETWEEN 0 AND 365",
            name="ck_organization_settings_onb_reopen_window",
        ),
        CheckConstraint(
            "onboarding_submission_payload_retention_days BETWEEN 1 AND 365",
            name="ck_organization_settings_onb_payload_retention",
        ),
        CheckConstraint(
            "NOT enable_attendance_module OR attendance_tracking_start_date IS NOT NULL",
            name="ck_organization_settings_att_start_required",
        ),
        CheckConstraint(
            "NOT enable_leave_module OR leave_tracking_start_date IS NOT NULL",
            name="ck_organization_settings_leave_start_required",
        ),
        CheckConstraint(
            "absolute_minimum_age_years BETWEEN 14 AND 18",
            name="ck_organization_settings_absolute_minimum_age",
        ),
        CheckConstraint(
            "absolute_minimum_age_years <= minimum_employment_age_years",
            name="ck_organization_settings_age_order",
        ),
        CheckConstraint(
            "backdated_salary_max_days BETWEEN 0 AND 3650",
            name="ck_organization_settings_backdate_days",
        ),
        CheckConstraint(
            "status_correction_window_days BETWEEN 0 AND 365",
            name="ck_organization_settings_correction_window",
        ),
        CheckConstraint(
            "document_expiry_warning_days BETWEEN 1 AND 365",
            name="ck_organization_settings_expiry_warning_days",
        ),
        # §7.5: a purpose statement of at least 30 characters is mandatory whenever religion or
        # social category collection is switched on. Enforcing it here means the flag and its
        # lawful basis cannot be separated by any write path (§25.2).
        CheckConstraint(
            "collect_religion = 0 OR "
            "(religion_collection_purpose IS NOT NULL "
            "AND CHAR_LENGTH(religion_collection_purpose) >= 30)",
            name="ck_organization_settings_religion_purpose",
        ),
        CheckConstraint(
            "collect_social_category = 0 OR "
            "(social_category_collection_purpose IS NOT NULL "
            "AND CHAR_LENGTH(social_category_collection_purpose) >= 30)",
            name="ck_organization_settings_social_category_purpose",
        ),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid7, ForeignKey("organizations.id", ondelete="RESTRICT"), primary_key=True
    )

    org_pan_required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("1")
    )
    employee_code_assignment_point: Mapped[str] = mapped_column(
        String(20), nullable=False, default="ON_CREATE", server_default="ON_CREATE"
    )
    employee_id_date_basis: Mapped[str] = mapped_column(
        String(20), nullable=False, default="JOINING_DATE", server_default="JOINING_DATE"
    )
    allow_manual_employee_code: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    official_email_uniqueness_scope: Mapped[str] = mapped_column(
        String(20), nullable=False, default="ORGANIZATION", server_default="ORGANIZATION"
    )
    minimum_employment_age_years: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=18, server_default=text("18")
    )
    absolute_minimum_age_years: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=14, server_default=text("14")
    )
    require_father_name: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    require_emergency_contact: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )

    # ---- data minimisation defaults (§25.2): off until justified ----
    collect_disability_info: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    collect_religion: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    religion_collection_purpose: Mapped[str | None] = mapped_column(Text, nullable=True)
    collect_social_category: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    social_category_collection_purpose: Mapped[str | None] = mapped_column(Text, nullable=True)

    referral_requires_referrer: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("1")
    )
    require_primary_bank_account: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    salary_revision_approval_required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    allow_custom_salary_components: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    backdated_salary_max_days: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=365, server_default=text("365")
    )
    """§7.5: 0 disables backdating entirely."""

    status_correction_window_days: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=30, server_default=text("30")
    )
    document_expiry_warning_days: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=30, server_default=text("30")
    )
    allow_document_self_verification: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    audit_all_document_downloads: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("1")
    )
    """§7.5: downloads of restricted categories are audited regardless of this flag."""

    enable_employee_self_view: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    self_view_salary: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    date_display_format: Mapped[str] = mapped_column(
        String(10), nullable=False, default="DD-MM-YYYY", server_default="DD-MM-YYYY"
    )

    # ---- position / job architecture (JA-SPEC-001 §8.7, §8.8) ----
    require_position_headcount_approval: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    """JA §8.7 / JA-Q-07: when true, a position cannot be opened until an approved headcount
    change request confirms its capacity. Off by default so creating the first positions does
    not require an approver to exist, while *changing* capacity always does."""

    enable_headcount_reservations: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    """JA §8.8 / JA-Q-14: reservations stay off until an onboarding or recruitment
    integration can produce them."""

    # ---- onboarding, attendance and leave (addendum §9.1) ----
    enable_onboarding_module: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    enable_attendance_module: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    enable_leave_module: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    attendance_tracking_start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    """Required when attendance is enabled; immutable once any attendance record exists (ATT-01)."""

    leave_tracking_start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    """Required when leave is enabled; immutable once any leave row exists (LEV-04)."""

    leave_year_start_month: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=1, server_default=text("1")
    )
    default_weekly_off_pattern_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    """Soft reference: ``weekly_off_patterns`` is created by the attendance migration, which
    adds the composite foreign key (addendum §5.6 step 4)."""

    attendance_day_close_time: Mapped[time] = mapped_column(
        Time, nullable=False, default=time(12, 0), server_default=text("'12:00:00'")
    )
    onboarding_required_for_activation: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    onboarding_auto_complete: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    onboarding_reopen_window_days: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=30, server_default=text("30")
    )
    onboarding_submission_payload_retention_days: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=30, server_default=text("30")
    )
    enable_onboarding_self_service: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    enable_attendance_self_service: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    enable_leave_self_service: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )

    # ---- payroll (payroll addendum §4.1) ----
    enable_payroll_module: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    enable_payroll_self_service: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    enable_payroll_alumni_access: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("1")
    )


class OrganizationFile(Base, PrimaryKeyColumn):
    """An organization-level file: a logo or a registration certificate (§23.2).

    Separate from ``employee_documents`` because these are not employee data and carry none of
    the category, verification or version machinery of §15. Like employee documents, only
    metadata lives here — the bytes are in private object storage and ``storage_key`` is never
    returned by the API.
    """

    __tablename__ = "organization_files"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_organization_files_org_id"),
        CheckConstraint("file_size_bytes > 0", name="ck_organization_files_size_positive"),
        CheckConstraint(
            "purpose IN ('LOGO','REGISTRATION_CERTIFICATE','PRIVACY_NOTICE','OTHER')",
            name="ck_organization_files_purpose",
        ),
        Index("ix_organization_files_organization_id_purpose", "organization_id", "purpose"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid7, ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    purpose: Mapped[str] = mapped_column(String(40), nullable=False)
    original_file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(500), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(100), nullable=False)
    file_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid7, ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=text("CURRENT_TIMESTAMP(6)")
    )
    archived_at: Mapped[datetime | None] = mapped_column(nullable=True)
