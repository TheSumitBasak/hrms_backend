"""Organization registrations, identifier policies and nomination configs (§7.4, §11.3, §13.3).

Kept apart from :mod:`app.services.organizations` because these are per-organization
*configuration* rows rather than the organization profile itself, and because the validation
rules differ sharply: registrations are typed and uniquely scoped, while policies and
nomination configs are small full-replacement sets.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from app.audit.actions import AuditAction
from app.audit.writer import AuditWriter
from app.core.clock import utc_now
from app.core.context import OrgContext
from app.core.errors import (
    DuplicateResource,
    ErrorDetail,
    GstinPanMismatch,
    ResourceNotFound,
    ValidationFailed,
)
from app.core.permissions import Permission
from app.db.types import uuid7
from app.models.org_config import (
    PURPOSE_REQUIRED_IDENTIFIERS,
    PURPOSE_STATEMENT_MIN_LENGTH,
    STATE_SCOPED_TYPES,
    STATE_UNIQUE_TYPES,
    SYSTEM_WIDE_UNIQUE_TYPES,
    IdentifierRequirementPolicy,
    NominationTypeConfig,
    OrganizationRegistration,
    RegistrationType,
)
from app.repositories.branch import BranchRepository
from app.repositories.organization import (
    IdentifierPolicyRepository,
    NominationConfigRepository,
    OrganizationRegistrationRepository,
    OrganizationRepository,
)
from app.repositories.reference import StateRepository
from app.services.organizations import assert_etag
from app.validation.normalizers import normalize_registration_number
from app.validation.rules import RuleId, check


@dataclass(frozen=True, slots=True)
class RegistrationWrite:
    registration_type: str
    registration_label: str | None
    registration_number: str
    branch_id: uuid.UUID | None
    state_code: str | None
    issuing_authority: str | None
    issue_date: date | None
    effective_from: date | None
    effective_to: date | None
    document_file_id: uuid.UUID | None
    remarks: str | None


TYPE_RULES: dict[RegistrationType, RuleId] = {
    RegistrationType.GSTIN: RuleId.GSTIN,
    RegistrationType.TAN: RuleId.TAN,
    RegistrationType.PF_ESTABLISHMENT: RuleId.PF_EST,
    RegistrationType.ESIC_EMPLOYER: RuleId.ESIC_EMP,
    RegistrationType.ESIC_SUB_CODE: RuleId.REGNO,
    RegistrationType.PT_REGISTRATION: RuleId.REGNO,
    RegistrationType.PT_ENROLMENT: RuleId.REGNO,
    RegistrationType.LWF: RuleId.REGNO,
    RegistrationType.SHOPS_ESTABLISHMENTS: RuleId.REGNO,
    RegistrationType.FACTORY_LICENSE: RuleId.REGNO,
    RegistrationType.CONTRACT_LABOUR_LICENSE: RuleId.REGNO,
    RegistrationType.OTHER: RuleId.REGNO,
}
"""The §7.4 validation column, as data: one rule per registration type."""


class OrganizationConfigService:
    """Configuration rows that hang off an organization (§7.4, §11.3, §13.3)."""

    def __init__(
        self,
        *,
        registrations: OrganizationRegistrationRepository,
        policies: IdentifierPolicyRepository,
        nomination_configs: NominationConfigRepository,
        organizations: OrganizationRepository,
        branches: BranchRepository,
        states: StateRepository,
        audit: AuditWriter,
    ) -> None:
        self._registrations = registrations
        self._policies = policies
        self._nomination_configs = nomination_configs
        self._organizations = organizations
        self._branches = branches
        self._states = states
        self._audit = audit

    # ---------------------------------------------------------------- registrations

    def list_registrations(
        self, context: OrgContext, *, registration_type: str | None = None
    ) -> Sequence[OrganizationRegistration]:
        context.require_any(Permission.ORG_MANAGE, Permission.EMPLOYEE_LIST)
        return self._registrations.list_registrations(registration_type=registration_type)

    def create_registration(
        self, context: OrgContext, *, write: RegistrationWrite
    ) -> OrganizationRegistration:
        context.require(Permission.ORG_MANAGE)
        organization = self._organization(context)
        write = _normalised_write(write)
        self._validate_registration(write, organization.pan)

        registration_type = RegistrationType(write.registration_type)
        duplicates = self._registrations.find_duplicates(
            registration_type=write.registration_type,
            registration_number=write.registration_number,
            state_code=write.state_code,
            registration_label=write.registration_label,
            system_wide=registration_type in SYSTEM_WIDE_UNIQUE_TYPES,
            state_unique=registration_type in STATE_UNIQUE_TYPES,
            label_unique=registration_type is RegistrationType.OTHER,
        )
        if duplicates:
            raise DuplicateResource(
                "A registration with that number already exists in its uniqueness scope."
            ).with_detail(
                field="registration_number",
                code="DUPLICATE",
                message="Registration numbers are unique within their §7.4 scope.",
            )

        registration = OrganizationRegistration(
            id=uuid7(),
            registration_type=write.registration_type,
            registration_label=write.registration_label,
            registration_number=write.registration_number,
            branch_id=write.branch_id,
            state_code=write.state_code,
            issuing_authority=write.issuing_authority,
            issue_date=write.issue_date,
            effective_from=write.effective_from,
            effective_to=write.effective_to,
            status="ACTIVE",
            document_file_id=write.document_file_id,
            remarks=write.remarks,
            created_by=context.user_id,
        )
        self._registrations.add(registration)
        self._registrations.flush()
        self._audit.record_change(
            context,
            action=AuditAction.REGISTRATION_CREATED,
            entity_type="organization_registration",
            entity_id=registration.id,
            after=_registration_values(registration),
            fields=list(_registration_values(registration)),
        )
        return registration

    def update_registration(
        self,
        context: OrgContext,
        *,
        registration_id: uuid.UUID,
        changes: dict[str, object],
        if_match: int | None,
    ) -> OrganizationRegistration:
        context.require(Permission.ORG_MANAGE)
        registration = self._registrations.require(registration_id, resource="registration")
        assert_etag(registration.row_version, if_match)

        organization = self._organization(context)
        write = RegistrationWrite(
            registration_type=registration.registration_type,
            registration_label=_optional_str(
                changes.get("registration_label"), registration.registration_label
            ),
            registration_number=registration.registration_number,
            branch_id=_optional_uuid(changes.get("branch_id"), registration.branch_id),
            state_code=_optional_str(changes.get("state_code"), registration.state_code),
            issuing_authority=_optional_str(
                changes.get("issuing_authority"), registration.issuing_authority
            ),
            issue_date=_optional_date(changes.get("issue_date"), registration.issue_date),
            effective_from=_optional_date(
                changes.get("effective_from"), registration.effective_from
            ),
            effective_to=_optional_date(changes.get("effective_to"), registration.effective_to),
            document_file_id=registration.document_file_id,
            remarks=_optional_str(changes.get("remarks"), registration.remarks),
        )
        self._validate_registration(write, organization.pan)

        before = {
            field: getattr(registration, field) for field in changes if field not in {"status"}
        }
        for field in (
            "registration_label",
            "branch_id",
            "state_code",
            "issuing_authority",
            "issue_date",
            "effective_from",
            "effective_to",
            "remarks",
        ):
            if field in changes:
                setattr(registration, field, getattr(write, field))
        if "status" in changes:
            registration.status = str(changes["status"])
        registration.updated_by = context.user_id

        after = {field: getattr(registration, field) for field in changes}
        self._audit.record_change(
            context,
            action=AuditAction.REGISTRATION_UPDATED,
            entity_type="organization_registration",
            entity_id=registration.id,
            before=before,
            after=after,
            fields=list(changes),
        )
        return registration

    # ---------------------------------------------------------------- identifier policies

    def list_identifier_policies(
        self, context: OrgContext
    ) -> Sequence[IdentifierRequirementPolicy]:
        context.require(Permission.ORG_MANAGE)
        return self._policies.list_policies()

    def replace_identifier_policies(
        self, context: OrgContext, *, rows: list[dict[str, object]]
    ) -> Sequence[IdentifierRequirementPolicy]:
        """Replace the §11.3 requirement rows supplied.

        The §25.2 data-minimisation rule is enforced here and by a database CHECK: Aadhaar,
        voter ID and driving licence may only be collected with a purpose statement of at
        least thirty characters.
        """
        context.require(Permission.ORG_MANAGE)
        details: list[ErrorDetail] = []
        existing = {row.identifier_type: row for row in self._policies.list_policies()}

        for row in rows:
            identifier_type = str(row["identifier_type"])
            requirement = str(row["requirement"])
            purpose = row.get("purpose_statement")
            needs_purpose = (
                identifier_type in PURPOSE_REQUIRED_TYPE_CODES
                and requirement != "NOT_COLLECTED"
                and (
                    not isinstance(purpose, str)
                    or len(purpose.strip()) < PURPOSE_STATEMENT_MIN_LENGTH
                )
            )
            if needs_purpose:
                details.append(
                    ErrorDetail(
                        f"{identifier_type}.purpose_statement",
                        "REQUIRED",
                        f"Collecting {identifier_type} requires a purpose statement of at "
                        f"least {PURPOSE_STATEMENT_MIN_LENGTH} characters.",
                    )
                )
            if identifier_type not in existing:
                details.append(
                    ErrorDetail(
                        f"{identifier_type}",
                        "UNKNOWN_IDENTIFIER",
                        "No policy row exists for that identifier type.",
                    )
                )
        if details:
            raise ValidationFailed(details=details)

        updated: list[IdentifierRequirementPolicy] = []
        for row in rows:
            policy = existing[str(row["identifier_type"])]
            before = {
                "requirement": policy.requirement,
                "purpose_statement": policy.purpose_statement,
            }
            policy.requirement = str(row["requirement"])
            policy.purpose_statement = _optional_str(row.get("purpose_statement"), None)
            policy.updated_by = context.user_id
            after = {
                "requirement": policy.requirement,
                "purpose_statement": policy.purpose_statement,
            }
            if before != after:
                self._audit.record_change(
                    context,
                    action=AuditAction.IDENTIFIER_POLICY_UPDATED,
                    entity_type="identifier_requirement_policy",
                    entity_id=policy.id,
                    before=before,
                    after=after,
                    fields=["requirement", "purpose_statement"],
                )
            updated.append(policy)
        return updated

    # ---------------------------------------------------------------- nomination configs

    def list_nomination_configs(self, context: OrgContext) -> Sequence[NominationTypeConfig]:
        context.require(Permission.ORG_MANAGE)
        return self._nomination_configs.list_configs()

    def replace_nomination_configs(
        self, context: OrgContext, *, rows: list[dict[str, object]]
    ) -> Sequence[NominationTypeConfig]:
        context.require(Permission.ORG_MANAGE)
        existing = {row.nomination_type: row for row in self._nomination_configs.list_configs()}

        details = [
            ErrorDetail(
                str(row["nomination_type"]),
                "UNKNOWN_NOMINATION_TYPE",
                "No nomination config row exists for that type.",
            )
            for row in rows
            if str(row["nomination_type"]) not in existing
        ]
        if details:
            raise ValidationFailed(details=details)

        updated: list[NominationTypeConfig] = []
        for row in rows:
            config = existing[str(row["nomination_type"])]
            before = {
                "requires_full_allocation": config.requires_full_allocation,
                "requires_dob": config.requires_dob,
                "max_nominees": config.max_nominees,
                "scheme_rule_notes": config.scheme_rule_notes,
            }
            config.requires_full_allocation = bool(row["requires_full_allocation"])
            config.requires_dob = bool(row["requires_dob"])
            max_nominees = row.get("max_nominees")
            config.max_nominees = int(str(max_nominees)) if max_nominees is not None else None
            config.scheme_rule_notes = _optional_str(row.get("scheme_rule_notes"), None)
            config.updated_by = context.user_id
            after = {
                "requires_full_allocation": config.requires_full_allocation,
                "requires_dob": config.requires_dob,
                "max_nominees": config.max_nominees,
                "scheme_rule_notes": config.scheme_rule_notes,
            }
            if before != after:
                self._audit.record_change(
                    context,
                    action=AuditAction.NOMINATION_CONFIG_UPDATED,
                    entity_type="nomination_type_config",
                    entity_id=config.id,
                    before=before,
                    after=after,
                    fields=list(after),
                )
            updated.append(config)
        return updated

    # ---------------------------------------------------------------- validation

    def _organization(self, context: OrgContext):  # type: ignore[no-untyped-def]
        organization = self._organizations.get(context.organization_id)
        if organization is None or organization.archived_at is not None:
            raise ResourceNotFound("organization")
        return organization

    def _validate_registration(
        self, write: RegistrationWrite, organization_pan: str | None
    ) -> None:
        registration_type = RegistrationType(write.registration_type)
        details = [
            *self._registration_common_details(write, registration_type),
            *self._registration_state_details(write, registration_type),
            *self._registration_number_details(write, registration_type),
        ]

        if registration_type is RegistrationType.GSTIN:
            number = write.registration_number
            if write.state_code and number[:2] != write.state_code:
                details.append(
                    ErrorDetail(
                        "state_code",
                        "MISMATCH",
                        "For a GSTIN the state code must equal characters 1-2 of the number.",
                    )
                )
            if organization_pan and number[2:12] != organization_pan.upper():
                raise GstinPanMismatch()

        if details:
            raise ValidationFailed(details=details)

    def _registration_common_details(
        self, write: RegistrationWrite, registration_type: RegistrationType
    ) -> list[ErrorDetail]:
        details: list[ErrorDetail] = []
        if registration_type is RegistrationType.OTHER and not write.registration_label:
            details.append(
                ErrorDetail(
                    "registration_label",
                    "REQUIRED",
                    "A label is required for an OTHER registration.",
                )
            )
        if write.branch_id is not None and self._branches.get(write.branch_id) is None:
            details.append(
                ErrorDetail(
                    "branch_id",
                    "NOT_FOUND",
                    "That branch does not belong to this organization.",
                )
            )
        if write.issue_date is not None and write.issue_date > utc_now().date():
            details.append(
                ErrorDetail("issue_date", "IN_FUTURE", "The issue date cannot be in the future.")
            )
        if (
            write.effective_to is not None
            and write.effective_from is not None
            and write.effective_to <= write.effective_from
        ):
            details.append(
                ErrorDetail(
                    "effective_to",
                    "INVALID_INTERVAL",
                    "effective_to must be after effective_from.",
                )
            )
        return details

    def _registration_state_details(
        self, write: RegistrationWrite, registration_type: RegistrationType
    ) -> list[ErrorDetail]:
        details: list[ErrorDetail] = []
        if registration_type in STATE_SCOPED_TYPES and write.state_code is None:
            details.append(
                ErrorDetail(
                    "state_code",
                    "REQUIRED",
                    f"A state code is required for {registration_type.value}.",
                )
            )
        if write.state_code is not None and not self._states.code_exists(write.state_code):
            details.append(
                ErrorDetail("state_code", "UNKNOWN_STATE", "Must be a valid GST state code.")
            )
        return details

    def _registration_number_details(
        self, write: RegistrationWrite, registration_type: RegistrationType
    ) -> list[ErrorDetail]:
        outcome = check(TYPE_RULES[registration_type], write.registration_number)
        if outcome.ok:
            return []
        return [
            ErrorDetail(
                "registration_number",
                TYPE_RULES[registration_type].value,
                outcome.message,
            )
        ]

    # The caller holds the normalised number the validator produced; expose it so the endpoint
    # can persist exactly what was validated.
    @staticmethod
    def normalise_number(value: str) -> str:
        normalized = normalize_registration_number(value)
        return normalized or value.strip().upper()


PURPOSE_REQUIRED_TYPE_CODES: frozenset[str] = frozenset(
    member.value for member in PURPOSE_REQUIRED_IDENTIFIERS
)


def _normalised_write(write: RegistrationWrite) -> RegistrationWrite:
    """Normalise the number before validation and persistence (§7.4)."""
    return RegistrationWrite(
        registration_type=write.registration_type,
        registration_label=write.registration_label,
        registration_number=normalize_registration_number(write.registration_number)
        or write.registration_number.strip().upper(),
        branch_id=write.branch_id,
        state_code=write.state_code,
        issuing_authority=write.issuing_authority,
        issue_date=write.issue_date,
        effective_from=write.effective_from,
        effective_to=write.effective_to,
        document_file_id=write.document_file_id,
        remarks=write.remarks,
    )


def _registration_values(registration: OrganizationRegistration) -> dict[str, object]:
    return {
        "registration_type": registration.registration_type,
        "registration_label": registration.registration_label,
        "registration_number": registration.registration_number,
        "branch_id": registration.branch_id,
        "state_code": registration.state_code,
        "status": registration.status,
    }


def _optional_str(value: object, default: str | None) -> str | None:
    return default if value is None else str(value)


def _optional_uuid(value: object, default: uuid.UUID | None) -> uuid.UUID | None:
    return default if value is None else uuid.UUID(str(value))


def _optional_date(value: object, default: date | None) -> date | None:
    if value is None:
        return default
    return value if isinstance(value, date) else date.fromisoformat(str(value))


__all__ = ["OrganizationConfigService", "RegistrationWrite"]
