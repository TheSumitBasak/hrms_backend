"""Organization, settings and group use cases (§7, §24.4.1).

The interesting work here is the §7.3 identifier-applicability matrix and the §21.3 warning
protocol. §7.3 draws a line that is easy to get wrong: CIN and LLPIN applicability are
**errors** (a trust cannot hold a CIN), while the PAN holder-type character and the CIN
ownership segment are **warnings** — "government companies, subsidiaries of foreign companies
and other cases legitimately differ". Treating those as errors would make the system refuse
legitimate organizations.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from app.audit.actions import AuditAction
from app.audit.events import AuditEvent, build_change_set
from app.audit.writer import AuditWriter
from app.core.context import ActorContext, OrgContext
from app.core.errors import (
    DuplicateResource,
    ErrorDetail,
    PreconditionFailed,
    ResourceNotFound,
    ValidationFailed,
)
from app.core.permissions import Permission
from app.core.warnings import WarningCode, WarningCollector
from app.db.types import uuid7
from app.models.organization import (
    CIN_REQUIRED_TYPES,
    EXPECTED_CIN_OWNERSHIP_SEGMENT,
    EXPECTED_PAN_FOURTH_CHARACTER,
    NO_REGISTRY_IDENTIFIER_TYPES,
    EntityType,
    Organization,
    OrganizationGroup,
    OrganizationSettings,
)
from app.repositories.organization import (
    OrganizationGroupRepository,
    OrganizationRepository,
    OrganizationSettingsRepository,
)
from app.repositories.reference import StateRepository
from app.services.seed_data import seed_organization
from app.validation.rules import RuleId, check, pan_fourth_character, validate_url

CIN_OWNERSHIP_SEGMENT = slice(12, 15)
"""§7.3: the CIN ownership segment is characters 13-15 (zero-based 12:15)."""


@dataclass(frozen=True, slots=True)
class OrganizationWrite:
    """Normalised organization input, after validation.

    A separate value object rather than passing the request model down, so the service layer
    does not depend on the HTTP contract and the normalised (upper-cased, trimmed) values are
    what reach the model.
    """

    organization_code: str
    group_id: uuid.UUID | None
    legal_name: str
    display_name: str
    entity_type: str
    industry: str | None
    date_of_incorporation: object | None
    website: str | None
    cin: str | None
    llpin: str | None
    pan: str | None
    tan: str | None
    udyam_registration_number: str | None
    registered_address: dict[str, object]
    corporate_address: dict[str, object] | None
    official_phone: str | None
    official_email: str | None
    hr_contact_name: str | None
    hr_contact_email: str | None
    hr_contact_phone: str | None
    financial_year_start_month: int
    default_currency: str
    timezone: str
    employee_id_format_settings: dict[str, object] | None


ALLOWED_ID_FORMAT_KEYS: frozenset[str] = frozenset(
    {
        "default_separator",
        "default_sequence_padding_length",
        "default_sequence_start",
        "uppercase_codes",
    }
)
"""§7.2: "Allowed keys only ... Unknown keys -> 422"."""

ALLOWED_SEPARATORS: frozenset[str] = frozenset({"-", "/", "_", ""})

ID_FORMAT_VALUE_RULES: dict[str, tuple[str, str, Callable[[object], bool]]] = {
    "default_separator": (
        "INVALID",
        "Must be one of '-', '/', '_' or an empty string.",
        lambda value: isinstance(value, str) and value in ALLOWED_SEPARATORS,
    ),
    "default_sequence_padding_length": (
        "OUT_OF_RANGE",
        "Must be an integer between 1 and 10.",
        lambda value: isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 10,
    ),
    "default_sequence_start": (
        "OUT_OF_RANGE",
        "Must be an integer of at least 0.",
        lambda value: isinstance(value, int) and not isinstance(value, bool) and value >= 0,
    ),
    "uppercase_codes": (
        "INVALID",
        "Must be a boolean.",
        lambda value: isinstance(value, bool),
    ),
}
"""Per-key validation for the §7.2 ``employee_id_format_settings`` JSON.

A table rather than a branch per key: §7.2 lists exactly four allowed keys with exactly four
value rules, and expressing them as data keeps the validator a single comprehension. ``bool``
is excluded from the integer checks because Python treats ``True`` as ``1``, and
``"padding": true`` is a client mistake worth reporting rather than silently accepting."""


class OrganizationService:
    """Creates and maintains organizations (§7)."""

    def __init__(
        self,
        *,
        organizations: OrganizationRepository,
        settings: OrganizationSettingsRepository,
        groups: OrganizationGroupRepository,
        states: StateRepository,
        audit: AuditWriter,
    ) -> None:
        self._organizations = organizations
        self._settings = settings
        self._groups = groups
        self._states = states
        self._audit = audit

    # ---------------------------------------------------------------- reads

    def list_accessible(
        self,
        accessible_ids: Sequence[uuid.UUID],
        *,
        status: str | None = None,
        group_id: uuid.UUID | None = None,
        query: str | None = None,
    ) -> Sequence[Organization]:
        """§24.4.1 ``GET /orgs`` — only organizations the caller holds a grant in."""
        return self._organizations.list_accessible(
            accessible_ids, status=status, group_id=group_id, query=query
        )

    def get(self, organization_id: uuid.UUID) -> Organization:
        """Load one organization, or ``404``.

        Authorization is the caller's: the organization context has already established that
        the caller holds a grant here, so this method does not re-check it.
        """
        found = self._organizations.get(organization_id)
        if found is None or found.archived_at is not None:
            raise ResourceNotFound("organization")
        return found

    def get_settings(self, organization_id: uuid.UUID) -> OrganizationSettings:
        return self._settings.require(organization_id)

    # ---------------------------------------------------------------- create

    def create(
        self,
        actor: ActorContext,
        *,
        write: OrganizationWrite,
        org_pan_required: bool,
        acknowledged_warnings: list[str],
    ) -> tuple[Organization, list[str]]:
        """Create an organization and seed its configuration (§24.4.1).

        Returns the organization and the warning codes that were raised and acknowledged, for
        the response envelope (DEC-021).
        """
        warnings = WarningCollector(acknowledged_warnings)
        self._validate_identifiers(write, warnings, org_pan_required=org_pan_required)
        self._validate_address(write.registered_address, field_prefix="registered_address")
        if write.corporate_address:
            self._validate_address(write.corporate_address, field_prefix="corporate_address")
        self._validate_id_format_settings(write.employee_id_format_settings)

        if self._organizations.code_exists(write.organization_code):
            raise DuplicateResource("An organization with that code already exists.").with_detail(
                field="organization_code",
                code="DUPLICATE",
                message="Organization codes are unique system-wide.",
            )
        if write.group_id is not None and self._groups.get(write.group_id) is None:
            raise ValidationFailed("That organization group does not exist.").with_detail(
                field="group_id", code="NOT_FOUND", message="Unknown group."
            )

        # §7.2: duplicate legal names across organizations are a warning, not an error —
        # group companies legitimately share a trading name.
        warnings.add_if(
            self._legal_name_exists(write.legal_name),
            WarningCode.ORG_NAME_DUPLICATE,
        )
        # §7.2: PAN is unique system-wide except for proprietorships, where it is the
        # proprietor's own and may recur; that case is a warning rather than a conflict.
        if write.pan and write.entity_type == EntityType.PROPRIETORSHIP:
            warnings.add_if(self._pan_exists(write.pan), WarningCode.ORG_PAN_DUPLICATE)

        acknowledged = warnings.enforce()

        organization = Organization(
            id=uuid7(),
            organization_code=write.organization_code,
            group_id=write.group_id,
            legal_name=write.legal_name,
            display_name=write.display_name,
            entity_type=write.entity_type,
            industry=write.industry,
            date_of_incorporation=write.date_of_incorporation,
            website=write.website,
            status="ACTIVE",
            cin=write.cin,
            llpin=write.llpin,
            pan=write.pan,
            tan=write.tan,
            udyam_registration_number=write.udyam_registration_number,
            official_phone=write.official_phone,
            official_email=write.official_email,
            hr_contact_name=write.hr_contact_name,
            hr_contact_email=write.hr_contact_email,
            hr_contact_phone=write.hr_contact_phone,
            financial_year_start_month=write.financial_year_start_month,
            default_currency=write.default_currency,
            timezone=write.timezone,
            employee_id_format_settings=write.employee_id_format_settings,
            created_by=actor.user_id,
            **_address_columns("registered_address", write.registered_address),
            **_address_columns("corporate_address", write.corporate_address),
        )
        self._organizations.add(organization)
        self._organizations.flush()

        # §24.4.1: creation seeds settings, lookups, categories, employment types and
        # policies. In the same transaction, so a half-configured organization is unreachable.
        seed_organization(self._organizations.session, organization.id)
        seeded = self._settings.require(organization.id)
        seeded.org_pan_required = org_pan_required

        self._audit.record(
            _organization_event(
                actor,
                action=AuditAction.ORG_CREATED,
                organization_id=organization.id,
                after={
                    "organization_code": organization.organization_code,
                    "legal_name": organization.legal_name,
                    "display_name": organization.display_name,
                    "entity_type": organization.entity_type,
                    "status": organization.status,
                },
                acknowledged_warnings=acknowledged,
            )
        )
        return organization, acknowledged

    # ---------------------------------------------------------------- update

    def update(
        self,
        context: OrgContext,
        *,
        changes: dict[str, object],
        addresses: dict[str, dict[str, object] | None],
        if_match: int | None,
        acknowledged_warnings: list[str],
    ) -> tuple[Organization, list[str]]:
        """Edit an organization profile (§24.4.1 ``PATCH /orgs/{orgId}``)."""
        context.require(Permission.ORG_MANAGE)
        organization = self.get(context.organization_id)
        assert_etag(organization.row_version, if_match)

        warnings = WarningCollector(acknowledged_warnings)
        before = {key: getattr(organization, key) for key in changes}

        self._normalise_changed_identifiers(changes)
        if (pan := changes.get("pan")) is not None:
            self._warn_on_pan_holder_type(str(pan), EntityType(organization.entity_type), warnings)

        # §7.7: changing the timezone or the financial-year start after codes have been issued
        # alters future {YEAR} tokens and day boundaries, so it needs acknowledgement.
        warnings.add_if(_changes_time_basis(organization, changes), WarningCode.ORG_TIMEZONE_CHANGE)

        for prefix, address in addresses.items():
            if address is not None:
                self._validate_address(address, field_prefix=prefix)

        if "employee_id_format_settings" in changes:
            raw_settings = changes["employee_id_format_settings"]
            self._validate_id_format_settings(
                raw_settings if isinstance(raw_settings, dict) else None
            )

        acknowledged = warnings.enforce()

        for field, value in changes.items():
            setattr(organization, field, value)
        for prefix, address in addresses.items():
            if address is not None:
                for column, value in _address_columns(prefix, address).items():
                    setattr(organization, column, value)
        organization.updated_by = context.user_id

        self._audit.record_change(
            context,
            action=AuditAction.ORG_UPDATED,
            entity_type="organization",
            entity_id=organization.id,
            before=before,
            after={key: getattr(organization, key) for key in changes},
            fields=list(changes),
            acknowledged_warnings=acknowledged,
        )
        return organization, acknowledged

    def set_status(
        self, context: OrgContext, *, active: bool, reason: str, if_match: int | None
    ) -> Organization:
        """Deactivate or reactivate (§7.7, §24.4.1).

        §7.7: an organization is never hard-deleted once anything references it. Deactivation
        blocks creating employees, rules, templates and documents while leaving existing data
        readable — enforcement of those blocks belongs to each of those services, which check
        ``is_active``.
        """
        context.require(Permission.ORG_ARCHIVE)
        organization = self.get(context.organization_id)
        assert_etag(organization.row_version, if_match)

        target = "ACTIVE" if active else "INACTIVE"
        if organization.status == target:
            raise ValidationFailed(f"The organization is already {target.lower()}.")

        before = {"status": organization.status}
        organization.status = target
        organization.updated_by = context.user_id

        self._audit.record_change(
            context,
            action=AuditAction.ORG_STATUS_CHANGED,
            entity_type="organization",
            entity_id=organization.id,
            before=before,
            after={"status": target},
            fields=["status"],
            reason=reason,
        )
        return organization

    # ---------------------------------------------------------------- settings

    def replace_settings(
        self, context: OrgContext, *, values: dict[str, object], if_match: int | None
    ) -> OrganizationSettings:
        """Replace the §7.5 settings (``PUT /orgs/{orgId}/settings``).

        A replacement, not a merge: §24.4.1 describes the endpoint as "Read / replace
        settings", so an omitted field returns to its specification default.
        """
        context.require(Permission.ORG_MANAGE)
        settings = self._settings.require(context.organization_id)
        if if_match is not None:
            # Settings have no row_version of their own (§7.5 makes them a 1:1 extension of
            # the organization), so concurrency is checked against the organization.
            assert_etag(self.get(context.organization_id).row_version, if_match)

        before = {key: getattr(settings, key) for key in values}
        for field, value in values.items():
            setattr(settings, field, value)

        self._audit.record_change(
            context,
            action=AuditAction.SETTINGS_UPDATED,
            entity_type="organization_settings",
            entity_id=context.organization_id,
            before=before,
            after=dict(values),
            fields=list(values),
        )
        return settings

    # ---------------------------------------------------------------- groups

    def list_groups(self) -> Sequence[OrganizationGroup]:
        return self._groups.list_all()

    def create_group(
        self, actor: ActorContext, *, group_code: str, group_name: str
    ) -> OrganizationGroup:
        """Create a group (§7.6).

        A group is also the **blind-index key scope** (§23.7): organizations in different
        groups derive different HMAC keys, so their identifier digests cannot match. Moving an
        organization between groups would therefore invalidate its blind indexes, which is why
        §7.6 offers no such operation and neither does this service.
        """
        if self._groups.get_by_code(group_code) is not None:
            raise DuplicateResource("A group with that code already exists.").with_detail(
                field="group_code", code="DUPLICATE", message="Group codes are unique."
            )
        group = OrganizationGroup(
            id=uuid7(),
            group_code=group_code,
            group_name=group_name,
            status="ACTIVE",
            created_by=actor.user_id,
        )
        self._groups.add(group)
        return group

    def update_group(
        self,
        actor: ActorContext,
        *,
        group_id: uuid.UUID,
        group_name: str | None,
        status: str | None,
        if_match: int | None,
    ) -> OrganizationGroup:
        group = self._groups.get(group_id)
        if group is None or group.archived_at is not None:
            raise ResourceNotFound("organization group")
        assert_etag(group.row_version, if_match)
        if group_name is not None:
            group.group_name = group_name
        if status is not None:
            group.status = status
        group.updated_by = actor.user_id
        return group

    def organization_count(self, group_id: uuid.UUID) -> int:
        return len(self._organizations.organizations_in_group(group_id))

    # ---------------------------------------------------------------- validation

    @staticmethod
    def _normalise_changed_identifiers(changes: dict[str, object]) -> None:
        """Validate and normalise whichever identifier fields a PATCH actually supplied.

        Driven by a table rather than a chain of ``if`` blocks, so adding a field is a row
        and the normalised value always replaces the submitted one — the stored form is what
        unique indexes compare (§21.1).
        """
        rules: tuple[tuple[str, RuleId], ...] = (
            ("pan", RuleId.PAN),
            ("tan", RuleId.TAN),
            ("udyam_registration_number", RuleId.UDYAM),
            ("official_phone", RuleId.PHONE),
            ("hr_contact_phone", RuleId.PHONE),
        )
        for field, rule in rules:
            value = changes.get(field)
            if value is None:
                continue
            outcome = check(rule, str(value))
            if not outcome.ok:
                raise _field_error(field, outcome.message)
            changes[field] = outcome.normalized

        website = changes.get("website")
        if website is not None:
            outcome = validate_url(str(website))
            if not outcome.ok:
                raise _field_error("website", outcome.message)
            changes["website"] = outcome.normalized

    def _validate_identifiers(
        self,
        write: OrganizationWrite,
        warnings: WarningCollector,
        *,
        org_pan_required: bool,
    ) -> None:
        """Apply §7.3 applicability and the §21.1 identifier patterns.

        Errors and warnings are split exactly as §7.3 does: applicability is an error, while a
        PAN holder-type or CIN ownership mismatch is a warning because legitimate exceptions
        exist.
        """
        entity_type = EntityType(write.entity_type)
        details = [
            *_registry_identifier_details(write, entity_type),
            *_identifier_pattern_details(write),
        ]
        if org_pan_required and not write.pan:
            details.append(
                ErrorDetail("pan", "REQUIRED", "A PAN is required by this organization policy.")
            )

        if details:
            raise ValidationFailed(details=details)

        if write.pan:
            self._warn_on_pan_holder_type(write.pan, entity_type, warnings)
        if write.cin:
            self._warn_on_cin_ownership(write.cin, entity_type, warnings)

    @staticmethod
    def _warn_on_pan_holder_type(
        pan: str, entity_type: EntityType, warnings: WarningCollector
    ) -> None:
        """§7.3 ``W-PAN-TYPE`` — a warning, never an error."""
        expected = EXPECTED_PAN_FOURTH_CHARACTER.get(entity_type)
        if expected is None:
            return
        found = pan_fourth_character(pan)
        warnings.add_if(
            found != expected,
            WarningCode.PAN_TYPE_MISMATCH,
            found=found,
            expected=expected,
            entity_type=entity_type.value,
        )

    @staticmethod
    def _warn_on_cin_ownership(
        cin: str, entity_type: EntityType, warnings: WarningCollector
    ) -> None:
        """§7.3 ``W-CIN-TYPE`` — a warning: subsidiaries of foreign companies differ."""
        expected = EXPECTED_CIN_OWNERSHIP_SEGMENT.get(entity_type)
        if expected is None or len(cin) < CIN_OWNERSHIP_SEGMENT.stop:
            return
        found = cin[CIN_OWNERSHIP_SEGMENT]
        warnings.add_if(
            found != expected,
            WarningCode.CIN_TYPE_MISMATCH,
            found=found,
            expected=expected,
            entity_type=entity_type.value,
        )

    def _validate_address(self, address: dict[str, object], *, field_prefix: str) -> None:
        """Apply the §7.1 address rules and derive the GST state code for India."""
        country = str(address.get("country") or "IN")
        details: list[ErrorDetail] = []

        postal = address.get("postal_code")
        outcome = check(RuleId.POSTAL, str(postal) if postal else None, country=country)
        if not outcome.ok:
            details.append(
                ErrorDetail(f"{field_prefix}.postal_code", RuleId.POSTAL.value, outcome.message)
            )
        else:
            address["postal_code"] = outcome.normalized

        if country == "IN":
            state_name = str(address.get("state") or "")
            state = self._states.get_by_name(state_name)
            if state is None:
                details.append(
                    ErrorDetail(
                        f"{field_prefix}.state",
                        "UNKNOWN_STATE",
                        "Must match an Indian state or union territory.",
                    )
                )
            else:
                supplied = address.get("state_code")
                if supplied and str(supplied) != state.code:
                    details.append(
                        ErrorDetail(
                            f"{field_prefix}.state_code",
                            "MISMATCH",
                            f"The GST state code for {state.name} is {state.code}.",
                        )
                    )
                else:
                    # §7.1 marks state_code derived for India, so it is filled in rather
                    # than demanded of the client.
                    address["state_code"] = state.code

        if details:
            raise ValidationFailed(details=details)

    @staticmethod
    def _validate_id_format_settings(settings: dict[str, object] | None) -> None:
        """§7.2: a strict allowlist of keys, with unknown keys rejected.

        These values only pre-fill new ID rules; §19.6 generation never reads them, which is
        why a bad value here is a validation error rather than a generation failure later.
        """
        if not settings:
            return
        unknown = sorted(set(settings) - ALLOWED_ID_FORMAT_KEYS)
        if unknown:
            raise ValidationFailed(
                "employee_id_format_settings contains unknown keys."
            ).with_detail(
                field="employee_id_format_settings",
                code="UNKNOWN_KEYS",
                message=f"Allowed keys: {sorted(ALLOWED_ID_FORMAT_KEYS)}. Unknown: {unknown}.",
            )

        details = [
            ErrorDetail(f"employee_id_format_settings.{key}", code, message)
            for key, (code, message, is_valid) in ID_FORMAT_VALUE_RULES.items()
            if settings.get(key) is not None and not is_valid(settings[key])
        ]
        if details:
            raise ValidationFailed(details=details)

    def _legal_name_exists(self, legal_name: str) -> bool:
        return any(
            organization.legal_name.casefold() == legal_name.casefold()
            for organization in self._organizations.list_by_legal_name(legal_name)
        )

    def _pan_exists(self, pan: str) -> bool:
        return self._organizations.pan_exists(pan)


def _registry_identifier_details(
    write: OrganizationWrite, entity_type: EntityType
) -> list[ErrorDetail]:
    """The §7.3 CIN/LLPIN applicability matrix, as errors.

    Applicability is an **error** — a trust cannot hold a CIN — unlike the PAN holder-type and
    CIN ownership-segment checks, which §7.3 makes warnings because legitimate exceptions
    exist. Expressed as a table of (required, forbidden) per entity type so the matrix is
    readable against the specification rather than buried in branches.
    """
    if entity_type in CIN_REQUIRED_TYPES:
        required = "cin"
    elif entity_type is EntityType.LLP:
        required = "llpin"
    elif entity_type in NO_REGISTRY_IDENTIFIER_TYPES:
        required = ""
    else:  # pragma: no cover - EntityType is exhaustively covered above
        return []

    details: list[ErrorDetail] = []
    if required and not getattr(write, required):
        details.append(
            ErrorDetail(
                required, "REQUIRED", f"A {required.upper()} is required for {entity_type.value}."
            )
        )
    for field in ("cin", "llpin"):
        if field != required and getattr(write, field):
            details.append(
                ErrorDetail(
                    field, "NOT_ALLOWED", f"{entity_type.value} does not have a {field.upper()}."
                )
            )
    return details


def _identifier_pattern_details(write: OrganizationWrite) -> list[ErrorDetail]:
    """Apply the §21.1 patterns to every identifier and contact field that was supplied."""
    checks: tuple[tuple[str, RuleId], ...] = (
        ("cin", RuleId.CIN),
        ("llpin", RuleId.LLPIN),
        ("pan", RuleId.PAN),
        ("tan", RuleId.TAN),
        ("udyam_registration_number", RuleId.UDYAM),
        ("official_phone", RuleId.PHONE),
        ("hr_contact_phone", RuleId.PHONE),
    )
    details: list[ErrorDetail] = []
    for field, rule in checks:
        value = getattr(write, field)
        if not value:
            continue
        outcome = check(rule, str(value))
        if not outcome.ok:
            details.append(ErrorDetail(field, rule.value, outcome.message))

    if write.website:
        outcome = validate_url(write.website)
        if not outcome.ok:
            details.append(ErrorDetail("website", RuleId.URL.value, outcome.message))
    return details


def assert_etag(current_version: int, if_match: int | None) -> None:
    """Enforce optimistic concurrency (§24.1, §20.4, E-30).

    ``If-Match`` is required on an update: without it a client that read a resource minutes
    ago could overwrite someone else's change silently, which is exactly the lost update
    ``row_version`` exists to prevent. A stale value is ``412 PRECONDITION_FAILED``.
    """
    if if_match is None:
        raise PreconditionFailed(
            "This request requires an If-Match header carrying the ETag from a prior GET."
        )
    if if_match != current_version:
        raise PreconditionFailed()


def _field_error(field: str, message: str) -> ValidationFailed:
    return ValidationFailed(details=[ErrorDetail(field, "INVALID", message)])


def _changes_time_basis(organization: Organization, changes: dict[str, object]) -> bool:
    """Whether a change alters what "today" or a ``{YEAR}`` token means (§7.7)."""
    timezone_changed = "timezone" in changes and changes["timezone"] != organization.timezone
    year_changed = (
        "financial_year_start_month" in changes
        and changes["financial_year_start_month"] != organization.financial_year_start_month
    )
    return timezone_changed or year_changed


def _address_columns(prefix: str, address: dict[str, object] | None) -> dict[str, object | None]:
    """Flatten an address group onto its prefixed columns (§7.1)."""
    fields = (
        "line_1",
        "line_2",
        "city",
        "district",
        "state",
        "state_code",
        "country",
        "postal_code",
    )
    if address is None:
        return {f"{prefix}_{field}": None for field in fields}
    return {f"{prefix}_{field}": address.get(field) for field in fields}


def _organization_event(
    actor: ActorContext,
    *,
    action: AuditAction,
    organization_id: uuid.UUID,
    after: dict[str, object],
    acknowledged_warnings: list[str],
) -> AuditEvent:
    return AuditEvent(
        action=action,
        entity_type="organization",
        entity_id=organization_id,
        actor_user_id=actor.user_id,
        actor_role_codes="SSA",
        organization_id=organization_id,
        changes=build_change_set(None, after),
        acknowledged_warnings=acknowledged_warnings,
        request_id=actor.metadata.request_id,
        ip_address=actor.metadata.ip_address,
        user_agent=actor.metadata.user_agent,
    )


__all__ = ["OrganizationService", "OrganizationWrite", "assert_etag"]
