"""Organization, settings and group endpoints (§24.4.1).

Two routers, because the two have different scoping:

* ``/orgs`` and ``/orgs/{org_id}/…`` — organization-scoped. Everything under ``{org_id}``
  resolves an :class:`~app.core.context.OrgContext` first, so the tenant boundary is
  established before a handler runs.
* ``/org-groups`` — platform-scoped, requiring ``org.create`` (§5.3 grants it to SSA only).

``POST /orgs`` is deliberately *not* organization-scoped: there is no organization to be
scoped to yet, so it resolves the caller's ``org.create`` permission across their grants.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Response, status

from app.api.deps import (
    CurrentOrg,
    CurrentUser,
    DbSession,
    IfMatch,
    get_access_service,
    get_org_config_service,
    get_organization_service,
    get_registration_repository,
)
from app.core.context import ActorContext
from app.core.errors import PermissionDenied
from app.core.permissions import Permission
from app.db.session import unit_of_work
from app.models.organization import Organization, OrganizationGroup, OrganizationSettings
from app.repositories.organization import OrganizationRegistrationRepository
from app.schemas.common import WarningPayload, etag_for
from app.schemas.organization import (
    AddressGroupResponse,
    IdentifierPolicyRequest,
    IdentifierPolicyResponse,
    NominationConfigRequest,
    NominationConfigResponse,
    OrganizationCreateRequest,
    OrganizationGroupCreateRequest,
    OrganizationGroupResponse,
    OrganizationGroupUpdateRequest,
    OrganizationResponse,
    OrganizationSettingsRequest,
    OrganizationSettingsResponse,
    OrganizationStatusRequest,
    OrganizationSummary,
    OrganizationUpdateRequest,
    RegistrationCreateRequest,
    RegistrationResponse,
    RegistrationSummary,
    RegistrationUpdateRequest,
)
from app.services.access import AccessService
from app.services.org_config import OrganizationConfigService, RegistrationWrite
from app.services.organizations import OrganizationService, OrganizationWrite

router = APIRouter(prefix="/orgs", tags=["Organizations"])
group_router = APIRouter(prefix="/org-groups", tags=["Organization groups"])


# --------------------------------------------------------------------------- serialisation


def _address(organization: Organization, prefix: str) -> AddressGroupResponse | None:
    line_1 = getattr(organization, f"{prefix}_line_1")
    if not line_1:
        return None
    return AddressGroupResponse(
        line_1=line_1,
        line_2=getattr(organization, f"{prefix}_line_2"),
        city=getattr(organization, f"{prefix}_city"),
        district=getattr(organization, f"{prefix}_district"),
        state=getattr(organization, f"{prefix}_state"),
        state_code=getattr(organization, f"{prefix}_state_code"),
        country=getattr(organization, f"{prefix}_country") or "IN",
        postal_code=getattr(organization, f"{prefix}_postal_code"),
    )


def _to_response(
    organization: Organization,
    *,
    warnings: list[str] | None = None,
    registrations: list[RegistrationSummary] | None = None,
) -> OrganizationResponse:
    registered = _address(organization, "registered_address")
    if registered is None:  # pragma: no cover - the column is NOT NULL
        raise ValueError("an organization must have a registered address")
    return OrganizationResponse(
        id=organization.id,
        organization_code=organization.organization_code,
        group_id=organization.group_id,
        legal_name=organization.legal_name,
        display_name=organization.display_name,
        entity_type=organization.entity_type,
        industry=organization.industry,
        date_of_incorporation=organization.date_of_incorporation,
        website=organization.website,
        status=organization.status,
        cin=organization.cin,
        llpin=organization.llpin,
        pan=organization.pan,
        tan=organization.tan,
        udyam_registration_number=organization.udyam_registration_number,
        registered_address=registered,
        corporate_address=_address(organization, "corporate_address"),
        official_phone=organization.official_phone,
        official_email=organization.official_email,
        hr_contact_name=organization.hr_contact_name,
        hr_contact_email=organization.hr_contact_email,
        hr_contact_phone=organization.hr_contact_phone,
        financial_year_start_month=organization.financial_year_start_month,
        default_currency=organization.default_currency,
        timezone=organization.timezone,
        employee_id_format_settings=organization.employee_id_format_settings,
        row_version=organization.row_version,
        registrations=registrations or [],
        created_at=organization.created_at,
        updated_at=organization.updated_at,
        # §21.3: acknowledged warnings are echoed so a client can show what it accepted
        # (DEC-021). The payload carries codes only, never the matched data.
        warnings=[WarningPayload(code=code, field=None, message="") for code in warnings or []],
    )


def _settings_response(settings: OrganizationSettings) -> OrganizationSettingsResponse:
    return OrganizationSettingsResponse.model_validate(settings)


def _group_response(group: OrganizationGroup, organization_count: int) -> OrganizationGroupResponse:
    return OrganizationGroupResponse(
        id=group.id,
        group_code=group.group_code,
        group_name=group.group_name,
        status=group.status,
        row_version=group.row_version,
        organization_count=organization_count,
    )


def _etag(response: Response, row_version: int) -> None:
    """Attach the §24.1 strong ETag so a client can issue a safe conditional update."""
    response.headers["ETag"] = etag_for(row_version)


# --------------------------------------------------------------------------- guards


def require_platform_permission(
    permission: Permission,
) -> Any:
    """Assert the caller holds ``permission`` in **any** organization they can access.

    Needed for the handful of endpoints that are not under ``/orgs/{org_id}`` — creating an
    organization, managing groups — where there is no organization context yet. §5.3 grants
    ``org.create`` to the System Super Admin alone, so in practice this resolves to "is a
    platform administrator", but it is expressed as a permission check so the matrix stays the
    single source of truth.
    """

    def dependency(
        actor: CurrentUser,
        access: Annotated[AccessService, Depends(get_access_service)],
    ) -> ActorContext:
        for entry in access.organization_access(actor.user_id):
            if entry.permissions.has(permission):
                return actor
        raise PermissionDenied(f"This action requires the '{permission.value}' permission.")

    return dependency


# --------------------------------------------------------------------------- organizations


@router.get("", response_model=list[OrganizationSummary], summary="List accessible organizations")
def list_organizations(
    actor: CurrentUser,
    access: Annotated[AccessService, Depends(get_access_service)],
    service: Annotated[OrganizationService, Depends(get_organization_service)],
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    group_id: uuid.UUID | None = None,
    q: Annotated[str | None, Query(min_length=2, max_length=100)] = None,
) -> list[OrganizationSummary]:
    """§24.4.1: "List organizations accessible to caller".

    Any authenticated caller may call this; the result is bounded by their grants, so a user
    with none receives an empty list rather than a 403. That is the §6.1 isolation property
    expressed as a list: you see exactly the organizations you hold a grant in.
    """
    accessible = access.accessible_organization_ids(actor.user_id)
    organizations = service.list_accessible(
        accessible, status=status_filter, group_id=group_id, query=q
    )
    return [
        OrganizationSummary(
            id=organization.id,
            organization_code=organization.organization_code,
            display_name=organization.display_name,
            status=organization.status,
        )
        for organization in organizations
    ]


@router.post(
    "",
    response_model=OrganizationResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create an organization and seed its configuration",
)
def create_organization(
    payload: OrganizationCreateRequest,
    response: Response,
    db: DbSession,
    service: Annotated[OrganizationService, Depends(get_organization_service)],
    actor: Annotated[ActorContext, Depends(require_platform_permission(Permission.ORG_CREATE))],
) -> OrganizationResponse:
    """Create an organization (§24.4.1).

    Seeds settings, lookups, document categories, employment types, identifier policies and
    nomination configs in the same transaction, so the organization is immediately usable and
    never exists half-configured.

    Warnings (duplicate legal name, PAN holder-type mismatch, CIN ownership mismatch) follow
    the §21.3 protocol: the first call returns ``422 WARNINGS_REQUIRE_ACKNOWLEDGEMENT`` listing
    them, and the client resubmits with ``acknowledged_warnings``.
    """
    write = OrganizationWrite(
        organization_code=payload.organization_code,
        group_id=payload.group_id,
        legal_name=payload.legal_name,
        display_name=payload.display_name,
        entity_type=payload.entity_type.value,
        industry=payload.industry,
        date_of_incorporation=payload.date_of_incorporation,
        website=payload.website,
        cin=payload.cin,
        llpin=payload.llpin,
        pan=payload.pan,
        tan=payload.tan,
        udyam_registration_number=payload.udyam_registration_number,
        registered_address=payload.registered_address.model_dump(),
        corporate_address=(
            payload.corporate_address.model_dump() if payload.corporate_address else None
        ),
        official_phone=payload.official_phone,
        official_email=payload.official_email,
        hr_contact_name=payload.hr_contact_name,
        hr_contact_email=payload.hr_contact_email,
        hr_contact_phone=payload.hr_contact_phone,
        financial_year_start_month=payload.financial_year_start_month,
        default_currency=payload.default_currency,
        timezone=payload.timezone,
        employee_id_format_settings=payload.employee_id_format_settings,
    )
    with unit_of_work(db):
        organization, acknowledged = service.create(
            actor,
            write=write,
            org_pan_required=payload.org_pan_required,
            acknowledged_warnings=payload.acknowledged_warnings,
        )
    _etag(response, organization.row_version)
    return _to_response(organization, warnings=acknowledged)


@router.get("/{org_id}", response_model=OrganizationResponse, summary="Get an organization")
def read_organization(
    response: Response,
    context: CurrentOrg,
    service: Annotated[OrganizationService, Depends(get_organization_service)],
    registrations: Annotated[
        OrganizationRegistrationRepository, Depends(get_registration_repository)
    ],
) -> OrganizationResponse:
    """§24.4.1: readable with ``employee.list`` or ``org.manage``.

    The organization context has already established that the caller holds a grant here; an
    id belonging to another tenant never reaches this handler.
    """
    context.require_any(Permission.EMPLOYEE_LIST, Permission.ORG_MANAGE)
    organization = service.get(context.organization_id)
    _etag(response, organization.row_version)
    rows = [
        RegistrationSummary(
            registration_type=row.registration_type,
            registration_label=row.registration_label,
            registration_number=row.registration_number,
            state_code=row.state_code,
            status=row.status,
            branch_id=row.branch_id,
        )
        for row in registrations.list_registrations()
    ]
    return _to_response(organization, registrations=rows)


@router.patch("/{org_id}", response_model=OrganizationResponse, summary="Edit an organization")
def update_organization(
    payload: OrganizationUpdateRequest,
    response: Response,
    context: CurrentOrg,
    if_match: IfMatch,
    db: DbSession,
    service: Annotated[OrganizationService, Depends(get_organization_service)],
) -> OrganizationResponse:
    """Edit a profile (§24.4.1). Requires ``If-Match`` (§24.1)."""
    submitted = payload.model_dump(
        exclude_unset=True,
        exclude={"registered_address", "corporate_address", "acknowledged_warnings"},
    )
    addresses = {
        "registered_address": (
            payload.registered_address.model_dump() if payload.registered_address else None
        ),
        "corporate_address": (
            payload.corporate_address.model_dump() if payload.corporate_address else None
        ),
    }
    with unit_of_work(db):
        organization, acknowledged = service.update(
            context,
            changes=submitted,
            addresses=addresses,
            if_match=if_match,
            acknowledged_warnings=payload.acknowledged_warnings,
        )
    _etag(response, organization.row_version)
    return _to_response(organization, warnings=acknowledged)


@router.post(
    "/{org_id}/deactivate",
    response_model=OrganizationResponse,
    summary="Deactivate an organization",
)
def deactivate_organization(
    payload: OrganizationStatusRequest,
    response: Response,
    context: CurrentOrg,
    if_match: IfMatch,
    db: DbSession,
    service: Annotated[OrganizationService, Depends(get_organization_service)],
) -> OrganizationResponse:
    """§7.7: blocks new employees, rules, templates and documents; existing data stays readable.

    Never a delete: §7.7 forbids hard deletion once anything references the organization.
    """
    with unit_of_work(db):
        organization = service.set_status(
            context, active=False, reason=payload.reason, if_match=if_match
        )
    _etag(response, organization.row_version)
    return _to_response(organization)


@router.post(
    "/{org_id}/reactivate",
    response_model=OrganizationResponse,
    summary="Reactivate an organization",
)
def reactivate_organization(
    payload: OrganizationStatusRequest,
    response: Response,
    context: CurrentOrg,
    if_match: IfMatch,
    db: DbSession,
    service: Annotated[OrganizationService, Depends(get_organization_service)],
) -> OrganizationResponse:
    """Return an organization to service (§7.7, E-32)."""
    with unit_of_work(db):
        organization = service.set_status(
            context, active=True, reason=payload.reason, if_match=if_match
        )
    _etag(response, organization.row_version)
    return _to_response(organization)


# --------------------------------------------------------------------------- settings


@router.get(
    "/{org_id}/settings",
    response_model=OrganizationSettingsResponse,
    summary="Read organization settings",
)
def read_settings(
    context: CurrentOrg,
    service: Annotated[OrganizationService, Depends(get_organization_service)],
) -> OrganizationSettingsResponse:
    """The §7.5 settings, which drive validation across the rest of the API."""
    context.require(Permission.ORG_MANAGE)
    return _settings_response(service.get_settings(context.organization_id))


@router.put(
    "/{org_id}/settings",
    response_model=OrganizationSettingsResponse,
    summary="Replace organization settings",
)
def replace_settings(
    payload: OrganizationSettingsRequest,
    context: CurrentOrg,
    if_match: IfMatch,
    db: DbSession,
    service: Annotated[OrganizationService, Depends(get_organization_service)],
) -> OrganizationSettingsResponse:
    """Replace the settings wholesale (§24.4.1).

    A ``PUT``: an omitted field returns to its §7.5 default rather than keeping its previous
    value. Enabling religion or social-category collection requires a purpose statement of at
    least thirty characters, enforced both here and by a database CHECK (§25.2).
    """
    with unit_of_work(db):
        settings = service.replace_settings(context, values=payload.model_dump(), if_match=if_match)
    return _settings_response(settings)


# --------------------------------------------------------------------------- groups


@group_router.get(
    "", response_model=list[OrganizationGroupResponse], summary="List organization groups"
)
def list_groups(
    service: Annotated[OrganizationService, Depends(get_organization_service)],
    actor: Annotated[ActorContext, Depends(require_platform_permission(Permission.ORG_CREATE))],
) -> list[OrganizationGroupResponse]:
    """§7.6 groups. Managed by a System Super Admin (``org.create``)."""
    return [
        _group_response(group, service.organization_count(group.id))
        for group in service.list_groups()
    ]


@group_router.post(
    "",
    response_model=OrganizationGroupResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create an organization group",
)
def create_group(
    payload: OrganizationGroupCreateRequest,
    db: DbSession,
    service: Annotated[OrganizationService, Depends(get_organization_service)],
    actor: Annotated[ActorContext, Depends(require_platform_permission(Permission.ORG_CREATE))],
) -> OrganizationGroupResponse:
    """Create a group (§7.6).

    A group is the **blind-index key scope** (§23.7), so which group an organization belongs
    to determines whether its identifier digests can ever match another's. That is why there
    is no "move organization between groups" operation: it would invalidate every blind index
    the organization has stored.
    """
    with unit_of_work(db):
        group = service.create_group(
            actor, group_code=payload.group_code, group_name=payload.group_name
        )
    return _group_response(group, 0)


@group_router.patch(
    "/{group_id}",
    response_model=OrganizationGroupResponse,
    summary="Edit an organization group",
)
def update_group(
    group_id: uuid.UUID,
    payload: OrganizationGroupUpdateRequest,
    if_match: IfMatch,
    db: DbSession,
    service: Annotated[OrganizationService, Depends(get_organization_service)],
    actor: Annotated[ActorContext, Depends(require_platform_permission(Permission.ORG_CREATE))],
) -> OrganizationGroupResponse:
    """Rename or deactivate a group. ``group_code`` is immutable (§7.6)."""
    with unit_of_work(db):
        group = service.update_group(
            actor,
            group_id=group_id,
            group_name=payload.group_name,
            status=payload.status,
            if_match=if_match,
        )
    return _group_response(group, service.organization_count(group.id))


# --------------------------------------------------------------------------- registrations


@router.get(
    "/{org_id}/registrations",
    response_model=list[RegistrationResponse],
    summary="List statutory registrations",
)
def list_registrations(
    context: CurrentOrg,
    service: Annotated[OrganizationConfigService, Depends(get_org_config_service)],
    registration_type: Annotated[str | None, Query(alias="type")] = None,
) -> list[RegistrationResponse]:
    """§24.4.1: readable with ``org.manage`` or ``employee.list``."""
    rows = service.list_registrations(context, registration_type=registration_type)
    return [RegistrationResponse.model_validate(row) for row in rows]


@router.post(
    "/{org_id}/registrations",
    response_model=RegistrationResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a statutory registration",
)
def create_registration(
    payload: RegistrationCreateRequest,
    context: CurrentOrg,
    db: DbSession,
    service: Annotated[OrganizationConfigService, Depends(get_org_config_service)],
) -> RegistrationResponse:
    """Create one §7.4 registration.

    Uniqueness follows the type's scope: GSTIN, PF and ESIC employer numbers collide across
    tenants; a GSTIN's first two characters must also equal its state code and characters
    3-12 must equal the organization PAN (``E-GSTIN-PAN``).
    """
    write = RegistrationWrite(
        registration_type=payload.registration_type.value,
        registration_label=payload.registration_label,
        registration_number=payload.registration_number,
        branch_id=payload.branch_id,
        state_code=payload.state_code,
        issuing_authority=payload.issuing_authority,
        issue_date=payload.issue_date,
        effective_from=payload.effective_from,
        effective_to=payload.effective_to,
        document_file_id=payload.document_file_id,
        remarks=payload.remarks,
    )
    with unit_of_work(db):
        registration = service.create_registration(context, write=write)
    return RegistrationResponse.model_validate(registration)


@router.patch(
    "/{org_id}/registrations/{registration_id}",
    response_model=RegistrationResponse,
    summary="Edit a statutory registration",
)
def update_registration(
    registration_id: uuid.UUID,
    payload: RegistrationUpdateRequest,
    context: CurrentOrg,
    if_match: IfMatch,
    db: DbSession,
    service: Annotated[OrganizationConfigService, Depends(get_org_config_service)],
) -> RegistrationResponse:
    """Edit a registration.

    The number is not editable: §24.4.1 requires a new row for a re-registration, so the
    history of what was registered when cannot be rewritten.
    """
    with unit_of_work(db):
        registration = service.update_registration(
            context,
            registration_id=registration_id,
            changes=payload.model_dump(exclude_unset=True),
            if_match=if_match,
        )
    return RegistrationResponse.model_validate(registration)


# --------------------------------------------------------------------------- identifier policies


@router.get(
    "/{org_id}/identifier-policies",
    response_model=list[IdentifierPolicyResponse],
    summary="Read identifier collection policies",
)
def read_identifier_policies(
    context: CurrentOrg,
    service: Annotated[OrganizationConfigService, Depends(get_org_config_service)],
) -> list[IdentifierPolicyResponse]:
    rows = service.list_identifier_policies(context)
    return [IdentifierPolicyResponse.model_validate(row) for row in rows]


@router.put(
    "/{org_id}/identifier-policies",
    response_model=list[IdentifierPolicyResponse],
    summary="Replace identifier collection policies",
)
def replace_identifier_policies(
    payload: list[IdentifierPolicyRequest],
    context: CurrentOrg,
    db: DbSession,
    service: Annotated[OrganizationConfigService, Depends(get_org_config_service)],
) -> list[IdentifierPolicyResponse]:
    """Replace §11.3 policies.

    Collecting Aadhaar, voter ID or driving licence requires a purpose statement of at least
    thirty characters (§25.2), enforced here and by a database CHECK.
    """
    rows = [item.model_dump() for item in payload]
    with unit_of_work(db):
        updated = service.replace_identifier_policies(context, rows=rows)
    return [IdentifierPolicyResponse.model_validate(row) for row in updated]


# --------------------------------------------------------------------------- nomination configs


@router.get(
    "/{org_id}/nomination-configs",
    response_model=list[NominationConfigResponse],
    summary="Read nomination configuration",
)
def read_nomination_configs(
    context: CurrentOrg,
    service: Annotated[OrganizationConfigService, Depends(get_org_config_service)],
) -> list[NominationConfigResponse]:
    rows = service.list_nomination_configs(context)
    return [NominationConfigResponse.model_validate(row) for row in rows]


@router.put(
    "/{org_id}/nomination-configs",
    response_model=list[NominationConfigResponse],
    summary="Replace nomination configuration",
)
def replace_nomination_configs(
    payload: list[NominationConfigRequest],
    context: CurrentOrg,
    db: DbSession,
    service: Annotated[OrganizationConfigService, Depends(get_org_config_service)],
) -> list[NominationConfigResponse]:
    rows = [item.model_dump() for item in payload]
    with unit_of_work(db):
        updated = service.replace_nomination_configs(context, rows=rows)
    return [NominationConfigResponse.model_validate(row) for row in updated]
