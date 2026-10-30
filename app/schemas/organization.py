"""Organization, settings and group contracts (§7, §24.4.1)."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Annotated, Any, Literal

from pydantic import Field, field_validator, model_validator

from app.core.masking import IdentifierType
from app.models.org_config import (
    IdentifierRequirement,
    NominationType,
    RegistrationType,
)
from app.models.organization import EntityType
from app.schemas.common import AcknowledgementMixin, ApiModel, EmailAddress, WarningPayload

CODE_PATTERN = r"^[A-Z][A-Z0-9]{1,9}$"
"""§7.2 / §7.6 organization and group code."""


class AddressGroup(ApiModel):
    """The §7.1 reusable address group.

    ``state_code`` is derived from the state through the ``states`` reference when the country
    is India, so a client may omit it; supplying a value that disagrees with the state is a
    validation error rather than a silent override.
    """

    line_1: Annotated[str, Field(min_length=3, max_length=200)]
    line_2: Annotated[str, Field(max_length=200)] | None = None
    city: Annotated[str, Field(min_length=2, max_length=100)]
    district: Annotated[str, Field(max_length=100)] | None = None
    state: Annotated[str, Field(min_length=2, max_length=100)]
    state_code: Annotated[str, Field(pattern=r"^[0-9]{2}$")] | None = None
    country: Annotated[str, Field(pattern=r"^[A-Z]{2}$")] = "IN"
    postal_code: Annotated[str, Field(min_length=3, max_length=10)]


class AddressGroupResponse(AddressGroup):
    """An address as returned, with the derived state code always populated for India."""


class OrganizationCreateRequest(AcknowledgementMixin):
    """``POST /orgs`` (§7.2, §24.4.1).

    Creating an organization also seeds its settings, lookups, document categories,
    employment types, identifier policies and nomination configs, all in the same
    transaction (§24.4.1), so an organization never exists half-configured.
    """

    organization_code: Annotated[str, Field(pattern=CODE_PATTERN)]
    group_id: uuid.UUID | None = None
    legal_name: Annotated[str, Field(min_length=2, max_length=200)]
    display_name: Annotated[str, Field(min_length=2, max_length=100)]
    entity_type: EntityType
    industry: Annotated[str, Field(max_length=50)] | None = None
    date_of_incorporation: date | None = None
    website: Annotated[str, Field(max_length=255)] | None = None

    cin: Annotated[str, Field(max_length=21)] | None = None
    llpin: Annotated[str, Field(max_length=8)] | None = None
    pan: Annotated[str, Field(max_length=10)] | None = None
    tan: Annotated[str, Field(max_length=10)] | None = None
    udyam_registration_number: Annotated[str, Field(max_length=19)] | None = None

    registered_address: AddressGroup
    corporate_address: AddressGroup | None = None

    official_phone: Annotated[str, Field(max_length=16)] | None = None
    official_email: EmailAddress | None = None
    hr_contact_name: Annotated[str, Field(max_length=150)] | None = None
    hr_contact_email: EmailAddress | None = None
    hr_contact_phone: Annotated[str, Field(max_length=16)] | None = None

    financial_year_start_month: Annotated[int, Field(ge=1, le=12)] = 4
    default_currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")] = "INR"
    timezone: Annotated[str, Field(max_length=50)] = "Asia/Kolkata"
    employee_id_format_settings: dict[str, Any] | None = None

    org_pan_required: bool = True
    """Seeds ``organization_settings.org_pan_required`` (§7.5, default true).

    Accepted here out of necessity: §7.2 makes the organization PAN conditional on this
    setting, but §7.5 settings can only be edited through ``PUT /orgs/{id}/settings``, which
    needs the organization to already exist. Without this an organization that genuinely has
    no PAN could never be created. See DECISIONS.md DEC-054.
    """

    @field_validator("organization_code")
    @classmethod
    def upper_code(cls, value: str) -> str:
        return value.upper()


class OrganizationUpdateRequest(AcknowledgementMixin):
    """``PATCH /orgs/{orgId}`` — a partial update of the §7.2 fields.

    ``organization_code`` is absent on purpose: §7.7 makes it immutable once any employee code
    has been issued, and employee codes embed it. The service rejects an attempt to change it
    after issuance; omitting it from the contract makes the common case unambiguous.
    """

    legal_name: Annotated[str, Field(min_length=2, max_length=200)] | None = None
    display_name: Annotated[str, Field(min_length=2, max_length=100)] | None = None
    industry: Annotated[str, Field(max_length=50)] | None = None
    date_of_incorporation: date | None = None
    website: Annotated[str, Field(max_length=255)] | None = None
    pan: Annotated[str, Field(max_length=10)] | None = None
    tan: Annotated[str, Field(max_length=10)] | None = None
    udyam_registration_number: Annotated[str, Field(max_length=19)] | None = None
    registered_address: AddressGroup | None = None
    corporate_address: AddressGroup | None = None
    official_phone: Annotated[str, Field(max_length=16)] | None = None
    official_email: EmailAddress | None = None
    hr_contact_name: Annotated[str, Field(max_length=150)] | None = None
    hr_contact_email: EmailAddress | None = None
    hr_contact_phone: Annotated[str, Field(max_length=16)] | None = None
    financial_year_start_month: Annotated[int, Field(ge=1, le=12)] | None = None
    default_currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")] | None = None
    timezone: Annotated[str, Field(max_length=50)] | None = None
    employee_id_format_settings: dict[str, Any] | None = None


class RegistrationSummary(ApiModel):
    registration_type: str
    registration_label: str | None
    registration_number: str
    state_code: str | None
    status: str
    branch_id: uuid.UUID | None


class OrganizationResponse(ApiModel):
    """A full organization profile."""

    id: uuid.UUID
    organization_code: str
    group_id: uuid.UUID | None
    legal_name: str
    display_name: str
    entity_type: str
    industry: str | None
    date_of_incorporation: date | None
    website: str | None
    status: str

    cin: str | None
    llpin: str | None
    pan: str | None
    tan: str | None
    udyam_registration_number: str | None

    registered_address: AddressGroupResponse
    corporate_address: AddressGroupResponse | None

    official_phone: str | None
    official_email: str | None
    hr_contact_name: str | None
    hr_contact_email: str | None
    hr_contact_phone: str | None

    financial_year_start_month: int
    default_currency: str
    timezone: str
    employee_id_format_settings: dict[str, Any] | None

    row_version: int
    registrations: list[RegistrationSummary] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
    warnings: list[WarningPayload] = Field(default_factory=list)


class OrganizationSummary(ApiModel):
    """``GET /orgs`` list item — §24.4.1 returns only these fields."""

    id: uuid.UUID
    organization_code: str
    display_name: str
    status: str


class OrganizationStatusRequest(ApiModel):
    """``POST /orgs/{orgId}/deactivate`` and ``/reactivate`` (§7.7)."""

    reason: Annotated[str, Field(min_length=10, max_length=500)]


class OrganizationSettingsRequest(ApiModel):
    """``PUT /orgs/{orgId}/settings`` — a full replacement of the §7.5 settings.

    A ``PUT`` of the whole object rather than a ``PATCH``, as §24.4.1 specifies ("Read /
    replace settings"). Defaults match §7.5 exactly, so omitting a field resets it to the
    specification default rather than leaving the previous value — which is what "replace"
    means and why the endpoint is not a PATCH.
    """

    org_pan_required: bool = True
    employee_code_assignment_point: Literal["ON_CREATE", "ON_ACTIVATION"] = "ON_CREATE"
    employee_id_date_basis: Literal["JOINING_DATE", "CREATION_DATE"] = "JOINING_DATE"
    allow_manual_employee_code: bool = False
    official_email_uniqueness_scope: Literal["ORGANIZATION", "GROUP", "NONE"] = "ORGANIZATION"
    minimum_employment_age_years: Annotated[int, Field(ge=14, le=21)] = 18
    absolute_minimum_age_years: Annotated[int, Field(ge=14, le=18)] = 14
    require_father_name: bool = False
    require_emergency_contact: bool = False

    collect_disability_info: bool = False
    collect_religion: bool = False
    religion_collection_purpose: Annotated[str, Field(max_length=2000)] | None = None
    collect_social_category: bool = False
    social_category_collection_purpose: Annotated[str, Field(max_length=2000)] | None = None

    referral_requires_referrer: bool = True
    require_primary_bank_account: bool = False
    salary_revision_approval_required: bool = False
    allow_custom_salary_components: bool = False
    backdated_salary_max_days: Annotated[int, Field(ge=0, le=3650)] = 365
    status_correction_window_days: Annotated[int, Field(ge=0, le=365)] = 30
    document_expiry_warning_days: Annotated[int, Field(ge=1, le=365)] = 30
    allow_document_self_verification: bool = False
    audit_all_document_downloads: bool = True
    enable_employee_self_view: bool = False
    self_view_salary: bool = False
    date_display_format: Literal["DD-MM-YYYY", "YYYY-MM-DD"] = "DD-MM-YYYY"

    # ---- position / job architecture (JA-SPEC-001 §8.7, §8.8) ----
    require_position_headcount_approval: bool = False
    """JA §8.7 / JA-Q-07: when true, a position cannot be opened until an approved headcount
    change request confirms its capacity. Off by default, so creating the first positions in a
    new organization does not require an approver to exist yet; *changing* capacity always
    does, regardless of this flag."""

    enable_headcount_reservations: bool = False
    """JA §8.8 / JA-Q-14: off until an onboarding or recruitment integration exists to create
    and release reservations. A capacity reducer with no producer would strand seats."""

    @model_validator(mode="after")
    def validate_age_order(self) -> OrganizationSettingsRequest:
        """§7.5: the absolute minimum must not exceed the employment minimum."""
        if self.absolute_minimum_age_years > self.minimum_employment_age_years:
            raise ValueError(
                "absolute_minimum_age_years must be at most minimum_employment_age_years"
            )
        return self

    @model_validator(mode="after")
    def validate_collection_purposes(self) -> OrganizationSettingsRequest:
        """§7.5: enabling religion or social category requires a ≥30-character purpose.

        Checked here as well as by a database CHECK, so a client gets a field-level error
        rather than a generic constraint failure. The point of both layers is that the flag
        and its lawful basis cannot be separated (§25.2, DEC-036).
        """
        minimum = 30
        if (
            self.collect_religion
            and len((self.religion_collection_purpose or "").strip()) < minimum
        ):
            raise ValueError(
                "religion_collection_purpose of at least 30 characters is required when "
                "collect_religion is true"
            )
        if (
            self.collect_social_category
            and len((self.social_category_collection_purpose or "").strip()) < minimum
        ):
            raise ValueError(
                "social_category_collection_purpose of at least 30 characters is required "
                "when collect_social_category is true"
            )
        return self


class OrganizationSettingsResponse(OrganizationSettingsRequest):
    """Settings as stored."""

    organization_id: uuid.UUID


class OrganizationGroupCreateRequest(ApiModel):
    """``POST /org-groups`` (§7.6)."""

    group_code: Annotated[str, Field(pattern=CODE_PATTERN)]
    group_name: Annotated[str, Field(min_length=2, max_length=150)]

    @field_validator("group_code")
    @classmethod
    def upper_code(cls, value: str) -> str:
        return value.upper()


class OrganizationGroupUpdateRequest(ApiModel):
    """``PATCH /org-groups/{id}`` — the code is immutable, so only the name and status."""

    group_name: Annotated[str, Field(min_length=2, max_length=150)] | None = None
    status: Literal["ACTIVE", "INACTIVE"] | None = None


class OrganizationGroupResponse(ApiModel):
    id: uuid.UUID
    group_code: str
    group_name: str
    status: str
    row_version: int
    organization_count: int = 0
    """How many organizations belong to this group. §7.6 says groups "have no data of their
    own", so this is the only thing worth reporting beyond the name."""


class RegistrationCreateRequest(ApiModel):
    """``POST /orgs/{orgId}/registrations`` (§7.4)."""

    registration_type: RegistrationType
    registration_label: Annotated[str, Field(max_length=100)] | None = None
    registration_number: Annotated[str, Field(min_length=1, max_length=50)]
    branch_id: uuid.UUID | None = None
    state_code: Annotated[str, Field(pattern=r"^[0-9]{2}$")] | None = None
    issuing_authority: Annotated[str, Field(max_length=150)] | None = None
    issue_date: date | None = None
    effective_from: date | None = None
    effective_to: date | None = None
    document_file_id: uuid.UUID | None = None
    remarks: Annotated[str, Field(max_length=500)] | None = None


class RegistrationUpdateRequest(ApiModel):
    """``PATCH /orgs/{orgId}/registrations/{id}``.

    ``registration_number`` is absent by design: §24.4.1 makes it immutable once the
    registration is ACTIVE, and re-registration is a new row.
    """

    registration_label: Annotated[str, Field(max_length=100)] | None = None
    branch_id: uuid.UUID | None = None
    state_code: Annotated[str, Field(pattern=r"^[0-9]{2}$")] | None = None
    issuing_authority: Annotated[str, Field(max_length=150)] | None = None
    issue_date: date | None = None
    effective_from: date | None = None
    effective_to: date | None = None
    status: Literal["ACTIVE", "INACTIVE"] | None = None
    document_file_id: uuid.UUID | None = None
    remarks: Annotated[str, Field(max_length=500)] | None = None


class RegistrationResponse(ApiModel):
    id: uuid.UUID
    organization_id: uuid.UUID
    branch_id: uuid.UUID | None
    registration_type: str
    registration_label: str | None
    registration_number: str
    state_code: str | None
    issuing_authority: str | None
    issue_date: date | None
    effective_from: date | None
    effective_to: date | None
    status: str
    document_file_id: uuid.UUID | None
    remarks: str | None
    row_version: int


class IdentifierPolicyRequest(ApiModel):
    """One §11.3 row in a ``PUT /orgs/{orgId}/identifier-policies`` replacement."""

    identifier_type: IdentifierType
    requirement: IdentifierRequirement
    purpose_statement: Annotated[str, Field(max_length=500)] | None = None


class IdentifierPolicyResponse(IdentifierPolicyRequest):
    id: uuid.UUID
    organization_id: uuid.UUID


class NominationConfigRequest(ApiModel):
    """One §13.3 row in a ``PUT /orgs/{orgId}/nomination-configs`` replacement."""

    nomination_type: NominationType
    requires_full_allocation: bool = True
    requires_dob: bool = False
    max_nominees: Annotated[int, Field(ge=1, le=20)] | None = None
    scheme_rule_notes: Annotated[str, Field(max_length=1000)] | None = None


class NominationConfigResponse(NominationConfigRequest):
    id: uuid.UUID
    organization_id: uuid.UUID


__all__ = [
    "AddressGroup",
    "AddressGroupResponse",
    "IdentifierPolicyRequest",
    "IdentifierPolicyResponse",
    "NominationConfigRequest",
    "NominationConfigResponse",
    "OrganizationCreateRequest",
    "OrganizationGroupCreateRequest",
    "OrganizationGroupResponse",
    "OrganizationGroupUpdateRequest",
    "OrganizationResponse",
    "OrganizationSettingsRequest",
    "OrganizationSettingsResponse",
    "OrganizationStatusRequest",
    "OrganizationSummary",
    "OrganizationUpdateRequest",
    "RegistrationCreateRequest",
    "RegistrationResponse",
    "RegistrationSummary",
    "RegistrationUpdateRequest",
]
