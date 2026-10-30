"""Request and response contracts for authentication and access management (§24.4.8)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, field_validator

from app.core.permissions import ORG_ADMIN_GRANTABLE_ROLES, Permission, Role
from app.core.security import MAX_PASSWORD_LENGTH, MIN_PASSWORD_LENGTH
from app.schemas.common import ApiModel, EmailAddress


class LoginRequest(ApiModel):
    """Local password sign-in."""

    email: EmailAddress
    password: Annotated[str, Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)]
    """No minimum length on *input*: rejecting a short password here would reveal the policy
    and would distinguish "too short" from "wrong", which §25.9 and OWASP A07 argue against.
    Length is enforced when a password is **set**, not when it is checked."""


class RefreshRequest(ApiModel):
    refresh_token: Annotated[str, Field(min_length=16, max_length=512)]


class LogoutRequest(ApiModel):
    refresh_token: Annotated[str, Field(min_length=16, max_length=512)]


class TokenResponse(ApiModel):
    """An issued token pair.

    The refresh token is returned in the body rather than a cookie: this is an API for
    service and SPA clients, and a cookie would additionally require CSRF defences (§25.9).
    """

    access_token: str
    refresh_token: str
    # S105 flags the field name, not a secret: "Bearer" is the RFC 6750 scheme.
    token_type: Literal["Bearer"] = "Bearer"  # noqa: S105
    expires_in: int = Field(description="Access token lifetime in seconds (≤900, NFR-13).")


class SessionPolicy(ApiModel):
    """Session limits the client should plan around (EMS NFR-13)."""

    idle_timeout_seconds: int
    absolute_timeout_seconds: int
    access_token_ttl_seconds: int


class CurrentUserResponse(ApiModel):
    """``GET /auth/me`` — who the caller is and where they have access.

    Deliberately returns organizations and permissions rather than roles alone, so a client
    can render its navigation without guessing at the §5.3 matrix. The server remains the
    only authority: §5.5 makes UI hiding cosmetic.
    """

    id: uuid.UUID
    email: EmailAddress
    display_name: str
    employee_id: uuid.UUID | None = None
    mfa_enabled: bool
    is_platform_admin: bool
    """True when the caller holds an active SSA grant, so clients need not match role names."""

    session: SessionPolicy
    organizations: list[OrganizationAccessSummary]


class OrganizationAccessSummary(ApiModel):
    """One organization the caller can act in, and what they may do there."""

    organization_id: uuid.UUID
    organization_code: str
    display_name: str
    roles: list[str]
    permissions: list[str]
    via_break_glass: bool = False
    expires_at: datetime | None = None


class UserCreateRequest(ApiModel):
    """``POST /users`` (§24.4.8)."""

    email: EmailAddress
    display_name: Annotated[str, Field(min_length=2, max_length=150)]
    employee_id: uuid.UUID | None = None
    auth_provider: Literal["LOCAL", "OIDC"] = "OIDC"
    password: (
        Annotated[str, Field(min_length=MIN_PASSWORD_LENGTH, max_length=MAX_PASSWORD_LENGTH)] | None
    ) = None
    """Required for a ``LOCAL`` account and forbidden otherwise, so an account intended to
    authenticate through the identity provider cannot quietly acquire a password."""

    mfa_enabled: bool = False

    @field_validator("password")
    @classmethod
    def password_only_for_local(cls, value: str | None) -> str | None:
        # Cross-field validation happens in the service, which has the provider in hand; this
        # validator only normalises an empty string to None.
        return value or None


class UserUpdateRequest(ApiModel):
    """``PATCH /users/{uid}`` — only the two fields §24.4.8 lists."""

    display_name: Annotated[str, Field(min_length=2, max_length=150)] | None = None
    employee_id: uuid.UUID | None = None


class UserResponse(ApiModel):
    id: uuid.UUID
    email: EmailAddress
    display_name: str
    employee_id: uuid.UUID | None
    auth_provider: str
    mfa_enabled: bool
    status: str
    last_login_at: datetime | None
    row_version: int


class DeactivateUserRequest(ApiModel):
    """``POST /users/{uid}/deactivate``. Revokes sessions and grants immediately (§25.5)."""

    reason: Annotated[str, Field(min_length=10, max_length=500)]


class GrantCreateRequest(ApiModel):
    """``POST /users/{uid}/org-access`` (§24.4.8)."""

    organization_id: uuid.UUID
    role_code: str
    expires_at: datetime | None = None
    reason: Annotated[str, Field(max_length=1000)] | None = None

    @field_validator("role_code")
    @classmethod
    def validate_role_code(cls, value: str) -> str:
        """Reject an unknown role before it reaches the service.

        ``SELF`` is excluded: §5.6 self-view is decided by the employee link, not by a grant,
        so granting it would imply an authority it does not carry.
        """
        try:
            role = Role(value.strip().upper())
        except ValueError as exc:
            grantable = ", ".join(sorted(role.value for role in ORG_ADMIN_GRANTABLE_ROLES))
            raise ValueError(f"Unknown role code. Expected one of: {grantable}, SSA.") from exc
        if role is Role.EMPLOYEE_SELF:
            raise ValueError(
                "The SELF role is not granted; employee self-view follows the user's "
                "employee link and the organization's enable_employee_self_view setting."
            )
        return role.value


class GrantResponse(ApiModel):
    id: uuid.UUID
    user_id: uuid.UUID
    organization_id: uuid.UUID
    role_code: str
    granted_by: uuid.UUID | None
    granted_at: datetime
    expires_at: datetime | None
    revoked_at: datetime | None
    is_break_glass: bool


class RevokeGrantRequest(ApiModel):
    reason: Annotated[str, Field(min_length=10, max_length=1000)]


class BreakGlassRequest(ApiModel):
    """``POST /break-glass`` (§24.4.8, §5.3 note 1).

    The minimum reason length and maximum duration are the spec's own limits, asserted here so
    a malformed request fails at the contract rather than inside the service.
    """

    organization_id: uuid.UUID
    reason: Annotated[str, Field(min_length=20, max_length=1000)]
    duration_minutes: Annotated[int, Field(ge=1, le=240)]


class BreakGlassResponse(ApiModel):
    grant_id: uuid.UUID
    organization_id: uuid.UUID
    expires_at: datetime
    reason: str


class RoleResponse(ApiModel):
    """``GET /roles``."""

    id: uuid.UUID
    role_code: str
    name: str
    description: str | None
    is_system: bool
    permissions: list[str]
    configurable_permissions: list[str]
    """The §5.3 ``C`` cells an Organization Admin may switch on for this role (DEC-004)."""


class PermissionResponse(ApiModel):
    """``GET /permissions``."""

    permission_code: str
    description: str


class RolePermissionsUpdateRequest(ApiModel):
    """``PUT /roles/{rid}/permissions`` — System Super Admin only (§24.4.8)."""

    permission_codes: list[str] = Field(max_length=len(Permission) + 1)

    @field_validator("permission_codes")
    @classmethod
    def validate_codes(cls, values: list[str]) -> list[str]:
        unknown = [code for code in values if code not in {item.value for item in Permission}]
        if unknown:
            raise ValueError(f"Unknown permission codes: {sorted(unknown)}")
        return sorted(set(values))


__all__ = [
    "BreakGlassRequest",
    "BreakGlassResponse",
    "CurrentUserResponse",
    "DeactivateUserRequest",
    "GrantCreateRequest",
    "GrantResponse",
    "LoginRequest",
    "LogoutRequest",
    "OrganizationAccessSummary",
    "PermissionResponse",
    "RefreshRequest",
    "RevokeGrantRequest",
    "RolePermissionsUpdateRequest",
    "RoleResponse",
    "TokenResponse",
    "UserCreateRequest",
    "UserResponse",
    "UserUpdateRequest",
]
