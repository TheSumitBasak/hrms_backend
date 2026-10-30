"""User, organization-access and break-glass endpoints (§24.4.8).

All of these require ``user.manage``, which §5.3 grants to System Super Admin and Organization
Admin. The two are *not* equivalent, and the difference is enforced in
:class:`app.services.access.AccessService`:

* an SSA manages platform-wide;
* an OA may grant only the roles of §5.3 note 4, only in organizations where it holds OA, and
  may never grant SSA.

``user.manage`` is a platform capability rather than an organization-scoped one, so these
routes sit outside ``/orgs/{org_id}`` and resolve the caller's own grants to decide what they
may do.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Response, status

from app.api.deps import (
    CurrentUser,
    DbSession,
    get_access_grant_repository,
    get_access_service,
    get_audit_writer,
    get_permission_repository,
    get_role_repository,
    get_user_repository,
)
from app.audit.actions import AuditAction
from app.audit.events import AuditEvent, build_change_set, format_actor_roles
from app.audit.writer import AuditWriter
from app.core.clock import utc_now
from app.core.errors import (
    DuplicateResource,
    PermissionDenied,
    ResourceNotFound,
    ValidationFailed,
)
from app.core.permissions import (
    Permission,
    Role,
    configurable_permissions_for,
)
from app.db.session import unit_of_work
from app.db.types import uuid7
from app.models.access import UserOrganizationAccess
from app.models.platform import AuthProvider, User
from app.repositories.platform import (
    AccessGrantRepository,
    PermissionRepository,
    RoleRepository,
    UserRepository,
)
from app.schemas.auth import (
    BreakGlassRequest,
    BreakGlassResponse,
    DeactivateUserRequest,
    GrantCreateRequest,
    GrantResponse,
    PermissionResponse,
    RevokeGrantRequest,
    RolePermissionsUpdateRequest,
    RoleResponse,
    UserCreateRequest,
    UserResponse,
    UserUpdateRequest,
)
from app.services.access import AccessService
from app.services.auth import AuthService
from app.validation.normalizers import normalize_email
from app.validation.rules import validate_name

router = APIRouter(tags=["Users and access"])


def _to_user_response(user: User) -> UserResponse:
    return UserResponse(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        employee_id=user.employee_id,
        auth_provider=user.auth_provider,
        mfa_enabled=user.mfa_enabled,
        status=user.status,
        last_login_at=user.last_login_at,
        row_version=user.row_version,
    )


def _to_grant_response(grant: UserOrganizationAccess, role_code: str) -> GrantResponse:
    return GrantResponse(
        id=grant.id,
        user_id=grant.user_id,
        organization_id=grant.organization_id,
        role_code=role_code,
        granted_by=grant.granted_by,
        granted_at=grant.granted_at,
        expires_at=grant.expires_at,
        revoked_at=grant.revoked_at,
        is_break_glass=grant.is_break_glass,
    )


class _ManagerContext:
    """What a ``user.manage`` caller is allowed to do, resolved once per request.

    Gathers the caller's own roles and the organizations where they hold ``user.manage``.
    Keeping it in one place means the §5.3 note 4 restriction is applied consistently by every
    route here rather than re-derived in each.
    """

    __slots__ = ("managed_organization_ids", "roles")

    def __init__(self, roles: frozenset[Role], managed_organization_ids: list[uuid.UUID]) -> None:
        self.roles = roles
        self.managed_organization_ids = managed_organization_ids

    @property
    def is_platform_admin(self) -> bool:
        return Role.SYSTEM_SUPER_ADMIN in self.roles


def require_user_manage(
    actor: CurrentUser,
    access: Annotated[AccessService, Depends(get_access_service)],
) -> _ManagerContext:
    """Assert the caller holds ``user.manage`` somewhere, and report where.

    A caller with no ``user.manage`` grant anywhere is refused outright. One with the
    permission in some organizations may act only there, which each route then enforces.
    """
    roles: set[Role] = set()
    managed: list[uuid.UUID] = []
    for entry in access.organization_access(actor.user_id):
        roles |= entry.permissions.roles
        if entry.permissions.has(Permission.USER_MANAGE):
            managed.append(entry.organization_id)

    if not managed:
        raise PermissionDenied("This action requires the 'user.manage' permission.")
    return _ManagerContext(frozenset(roles), managed)


ManagerContext = Annotated[_ManagerContext, Depends(require_user_manage)]


# --------------------------------------------------------------------------- users


@router.get("/users", response_model=list[UserResponse], summary="List manageable users")
def list_users(
    manager: ManagerContext,
    users: Annotated[UserRepository, Depends(get_user_repository)],
) -> list[UserResponse]:
    """List users.

    §24.4.8: "OA sees only users with grants in their organizations". An Organization Admin
    must not be able to enumerate the platform's user list, so the query is restricted to the
    organizations they manage.
    """
    found = users.list_for_organizations(manager.managed_organization_ids, limit=200)
    return [_to_user_response(user) for user in found]


@router.post(
    "/users",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a user account",
)
def create_user(
    payload: UserCreateRequest,
    actor: CurrentUser,
    manager: ManagerContext,
    db: DbSession,
    users: Annotated[UserRepository, Depends(get_user_repository)],
    audit: Annotated[AuditWriter, Depends(get_audit_writer)],
) -> UserResponse:
    """Create an account. Grants are separate — a new user can reach nothing until granted.

    A ``LOCAL`` account requires a password and an ``OIDC`` account must not have one, so an
    account meant to authenticate through the identity provider cannot quietly acquire a
    local credential.
    """
    email = normalize_email(payload.email)
    if email is None:
        raise ValidationFailed("An email address is required.")
    if users.get_by_email(email) is not None:
        raise DuplicateResource("A user with that email address already exists.")

    name_check = validate_name(payload.display_name, max_length=150)
    if not name_check.ok:
        raise ValidationFailed(name_check.message).with_detail(
            field="display_name", code="INVALID_NAME", message=name_check.message
        )

    is_local = payload.auth_provider == AuthProvider.LOCAL.value
    if is_local and not payload.password:
        raise ValidationFailed("A local account requires a password.").with_detail(
            field="password", code="REQUIRED", message="Required when auth_provider is LOCAL."
        )
    if not is_local and payload.password:
        raise ValidationFailed("A password may only be set on a local account.").with_detail(
            field="password",
            code="NOT_ALLOWED",
            message="Omit the password for an OIDC account.",
        )

    with unit_of_work(db):
        user = User(
            id=uuid7(),
            email=email,
            display_name=name_check.normalized or payload.display_name,
            employee_id=payload.employee_id,
            auth_provider=payload.auth_provider,
            password_hash=(
                AuthService.hash_new_password(payload.password)
                if is_local and payload.password
                else None
            ),
            mfa_enabled=payload.mfa_enabled,
            created_by=actor.user_id,
        )
        users.add(user)
        # A platform event: user creation belongs to no single organization, so §20.1
        # leaves organization_id null rather than attributing it to a placeholder.
        audit.record(
            _platform_event(
                actor,
                action=AuditAction.USER_CREATED,
                entity_id=user.id,
                roles=manager.roles,
                after={
                    "email": user.email,
                    "display_name": user.display_name,
                    "auth_provider": user.auth_provider,
                    "mfa_enabled": user.mfa_enabled,
                },
            )
        )
    return _to_user_response(user)


@router.patch("/users/{user_id}", response_model=UserResponse, summary="Edit a user")
def update_user(
    user_id: uuid.UUID,
    payload: UserUpdateRequest,
    actor: CurrentUser,
    manager: ManagerContext,
    db: DbSession,
    users: Annotated[UserRepository, Depends(get_user_repository)],
    audit: Annotated[AuditWriter, Depends(get_audit_writer)],
) -> UserResponse:
    """Edit the two fields §24.4.8 permits: display name and employee link."""
    user = users.get(user_id)
    if user is None or user.archived_at is not None:
        raise ResourceNotFound("user")

    before = {"display_name": user.display_name, "employee_id": user.employee_id}
    with unit_of_work(db):
        if payload.display_name is not None:
            name_check = validate_name(payload.display_name, max_length=150)
            if not name_check.ok:
                raise ValidationFailed(name_check.message).with_detail(
                    field="display_name", code="INVALID_NAME", message=name_check.message
                )
            user.display_name = name_check.normalized or payload.display_name
        if payload.employee_id is not None:
            user.employee_id = payload.employee_id
        user.updated_by = actor.user_id

        audit.record(
            _platform_event(
                actor,
                action=AuditAction.USER_UPDATED,
                entity_id=user.id,
                roles=manager.roles,
                before=before,
                after={"display_name": user.display_name, "employee_id": user.employee_id},
            )
        )
    return _to_user_response(user)


@router.post(
    "/users/{user_id}/deactivate",
    response_model=UserResponse,
    summary="Deactivate a user and revoke their access",
)
def deactivate_user(
    user_id: uuid.UUID,
    payload: DeactivateUserRequest,
    actor: CurrentUser,
    manager: ManagerContext,
    db: DbSession,
    users: Annotated[UserRepository, Depends(get_user_repository)],
    access: Annotated[AccessService, Depends(get_access_service)],
) -> UserResponse:
    """Deactivate an account.

    §24.4.8: "Revokes sessions and refresh tokens immediately". All three effects — inactive
    status, revoked refresh tokens, revoked grants — happen in one transaction, so a partial
    deactivation that leaves a live session behind is not reachable.
    """
    user = users.get(user_id)
    if user is None or user.archived_at is not None:
        raise ResourceNotFound("user")
    if user.id == actor.user_id:
        raise ValidationFailed("You cannot deactivate your own account.")

    with unit_of_work(db):
        access.deactivate_user(actor, user=user, reason=payload.reason, actor_roles=manager.roles)
    return _to_user_response(user)


# --------------------------------------------------------------------------- grants


@router.get(
    "/users/{user_id}/org-access",
    response_model=list[GrantResponse],
    summary="List a user's organization grants",
)
def list_grants(
    user_id: uuid.UUID,
    manager: ManagerContext,
    grants: Annotated[AccessGrantRepository, Depends(get_access_grant_repository)],
    roles: Annotated[RoleRepository, Depends(get_role_repository)],
) -> list[GrantResponse]:
    """List grants, including revoked ones (§23.2 retains them).

    An Organization Admin sees only grants in organizations they manage — otherwise the
    endpoint would disclose a user's access elsewhere on the platform.
    """
    role_codes = {row.id: row.role_code for row in roles.list_all()}
    visible = [
        grant
        for grant in grants.list_for_user(user_id)
        if manager.is_platform_admin
        or grant.organization_id in set(manager.managed_organization_ids)
    ]
    return [_to_grant_response(grant, role_codes.get(grant.role_id, "")) for grant in visible]


@router.post(
    "/users/{user_id}/org-access",
    response_model=GrantResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Grant a role in an organization",
)
def create_grant(
    user_id: uuid.UUID,
    payload: GrantCreateRequest,
    actor: CurrentUser,
    manager: ManagerContext,
    db: DbSession,
    access: Annotated[AccessService, Depends(get_access_service)],
    roles: Annotated[RoleRepository, Depends(get_role_repository)],
) -> GrantResponse:
    """Grant a role (§24.4.8).

    §5.3 note 4 is enforced in the service: an Organization Admin cannot grant SSA and cannot
    grant access to an organization it does not administer. Both are privilege-escalation
    paths, so they are checked where the grant is created rather than at the edge.
    """
    role = Role(payload.role_code)
    role_row = roles.get_by_code(role.value)
    if role_row is None:
        raise ValidationFailed(f"Unknown role code: {payload.role_code}")

    with unit_of_work(db):
        grant = access.grant(
            actor,
            user_id=user_id,
            organization_id=payload.organization_id,
            role=role,
            role_id=role_row.id,
            expires_at=payload.expires_at,
            reason=payload.reason,
            granting_roles=manager.roles,
            granting_organization_ids=manager.managed_organization_ids,
        )
    return _to_grant_response(grant, role.value)


@router.delete(
    "/users/{user_id}/org-access/{grant_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke an organization grant",
)
def revoke_grant(
    user_id: uuid.UUID,
    grant_id: uuid.UUID,
    payload: RevokeGrantRequest,
    actor: CurrentUser,
    manager: ManagerContext,
    db: DbSession,
    access: Annotated[AccessService, Depends(get_access_service)],
) -> Response:
    """Revoke a grant. Effective within 60 seconds by the §5.5 permission-cache ceiling."""
    with unit_of_work(db):
        grant = access.revoke(
            actor,
            grant_id=grant_id,
            reason=payload.reason,
            granting_roles=manager.roles,
            granting_organization_ids=manager.managed_organization_ids,
        )
        if grant.user_id != user_id:
            # The path must be self-consistent, and a mismatch must not reveal which user the
            # grant actually belongs to.
            raise ResourceNotFound("access grant")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------- break-glass


@router.post(
    "/break-glass",
    response_model=BreakGlassResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Time-bound emergency access to an organization",
)
def request_break_glass(
    payload: BreakGlassRequest,
    actor: CurrentUser,
    manager: ManagerContext,
    db: DbSession,
    access: Annotated[AccessService, Depends(get_access_service)],
    roles: Annotated[RoleRepository, Depends(get_role_repository)],
) -> BreakGlassResponse:
    """Mint a break-glass grant (§5.3 note 1).

    A System Super Admin has **no** employee-data access by default. This is the only way to
    obtain it: a grant of at most four hours with a reason of at least twenty characters. The
    grant and every access made under it are audited into the target organization's own log,
    so the organization can see that a platform administrator entered.
    """
    hr_admin = roles.get_by_code(AccessService.break_glass_role().value)
    if hr_admin is None:  # pragma: no cover - seeded by seed_platform
        raise ValidationFailed("The HR Admin role is not configured; run the platform seed.")

    with unit_of_work(db):
        grant = access.grant_break_glass(
            actor,
            organization_id=payload.organization_id,
            reason=payload.reason,
            duration_minutes=payload.duration_minutes,
            hr_admin_role_id=hr_admin.id,
            actor_roles=manager.roles,
        )
        expires_at = grant.expires_at or utc_now()
    return BreakGlassResponse(
        grant_id=grant.id,
        organization_id=grant.organization_id,
        expires_at=expires_at,
        reason=grant.reason or payload.reason,
    )


# --------------------------------------------------------------------------- catalogue


@router.get(
    "/roles",
    response_model=list[RoleResponse],
    summary="Role catalogue",
    dependencies=[Depends(require_user_manage)],
)
def list_roles(
    roles: Annotated[RoleRepository, Depends(get_role_repository)],
) -> list[RoleResponse]:
    """The §5.1 roles with their default and configurable permission sets.

    ``configurable_permissions`` are the §5.3 ``C`` cells an Organization Admin may switch on
    per organization (DEC-004), which a client needs in order to offer that choice at all.
    """
    results: list[RoleResponse] = []
    for row in roles.list_all():
        try:
            role = Role(row.role_code)
        except ValueError:
            continue
        results.append(
            RoleResponse(
                id=row.id,
                role_code=row.role_code,
                name=row.name,
                description=row.description,
                is_system=row.is_system,
                permissions=sorted(roles.permission_codes_for(row.id)),
                configurable_permissions=sorted(
                    permission.value for permission in configurable_permissions_for(role)
                ),
            )
        )
    return results


@router.get(
    "/permissions",
    response_model=list[PermissionResponse],
    summary="Permission catalogue",
    dependencies=[Depends(require_user_manage)],
)
def list_permissions(
    permissions: Annotated[PermissionRepository, Depends(get_permission_repository)],
) -> list[PermissionResponse]:
    """The §5.2 permission codes."""
    return [
        PermissionResponse(permission_code=row.permission_code, description=row.description)
        for row in permissions.list_all()
    ]


@router.put(
    "/roles/{role_id}/permissions",
    response_model=RoleResponse,
    summary="Replace a role's permissions",
)
def update_role_permissions(
    role_id: uuid.UUID,
    payload: RolePermissionsUpdateRequest,
    manager: ManagerContext,
    actor: CurrentUser,
    db: DbSession,
    roles: Annotated[RoleRepository, Depends(get_role_repository)],
    permissions: Annotated[PermissionRepository, Depends(get_permission_repository)],
    audit: Annotated[AuditWriter, Depends(get_audit_writer)],
) -> RoleResponse:
    """§24.4.8: System Super Admin only; the SSA role itself is immutable."""
    if not manager.is_platform_admin:
        raise PermissionDenied("Changing role permissions requires the SSA role.")
    role = roles.get(role_id) if hasattr(roles, "get") else None
    if role is None:
        matches = [row for row in roles.list_all() if row.id == role_id]
        role = matches[0] if matches else None
    if role is None:
        raise ResourceNotFound("role")
    if role.role_code == Role.SYSTEM_SUPER_ADMIN.value:
        raise PermissionDenied("The SSA role's permissions are immutable.")
    permission_ids = permissions.ids_for_codes(payload.permission_codes)
    if len(permission_ids) != len(set(payload.permission_codes)):
        raise ValidationFailed("One or more permission codes do not exist.")
    before = sorted(roles.permission_codes_for(role.id))
    with unit_of_work(db):
        roles.replace_permissions(role, permission_ids)
        audit.record(
            _platform_event(
                actor,
                action=AuditAction.ROLE_PERMISSIONS_CHANGED,
                entity_id=role.id,
                roles=manager.roles,
                before={"permissions": before},
                after={"permissions": sorted(payload.permission_codes)},
            )
        )
    return RoleResponse(
        id=role.id,
        role_code=role.role_code,
        name=role.name,
        description=role.description,
        is_system=role.is_system,
        permissions=sorted(payload.permission_codes),
        configurable_permissions=[],
    )


def _platform_event(
    actor: CurrentUser,
    *,
    action: AuditAction,
    entity_id: uuid.UUID,
    roles: frozenset[Role],
    before: Mapping[str, Any] | None = None,
    after: Mapping[str, Any] | None = None,
) -> AuditEvent:
    """Build an audit event for a platform-scoped action (§20.1: null organization)."""
    return AuditEvent(
        action=action,
        entity_type="user",
        entity_id=entity_id,
        actor_user_id=actor.user_id,
        actor_role_codes=format_actor_roles(roles),
        organization_id=None,
        changes=build_change_set(dict(before) if before else None, dict(after) if after else None),
        request_id=actor.metadata.request_id,
        ip_address=actor.metadata.ip_address,
        user_agent=actor.metadata.user_agent,
    )
