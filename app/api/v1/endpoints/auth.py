"""Authentication endpoints: login, refresh, logout, and who-am-I.

Handlers are thin by design (`fastapi-rules`): they parse, delegate to a service, and
serialise. There is no `Session` here, no query and no business rule.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, status

from app.api.deps import (
    CurrentUser,
    DbSession,
    get_access_service,
    get_app_settings,
    get_auth_service,
    get_request_metadata,
    get_user_repository,
)
from app.core.config import Settings
from app.core.context import RequestMetadata
from app.core.errors import ResourceNotFound
from app.core.permissions import Role
from app.db.session import unit_of_work
from app.repositories.platform import UserRepository
from app.schemas.auth import (
    CurrentUserResponse,
    LoginRequest,
    LogoutRequest,
    OrganizationAccessSummary,
    RefreshRequest,
    SessionPolicy,
    TokenResponse,
)
from app.services.access import AccessService
from app.services.auth import AuthService

router = APIRouter(prefix="/auth", tags=["Authentication"])


@router.post(
    "/login",
    response_model=TokenResponse,
    summary="Sign in with a local password",
    responses={
        401: {"description": "The email address or password is incorrect."},
        429: {"description": "Too many failed attempts (§25.9)."},
    },
)
def login(
    payload: LoginRequest,
    db: DbSession,
    service: Annotated[AuthService, Depends(get_auth_service)],
    metadata: Annotated[RequestMetadata, Depends(get_request_metadata)],
) -> TokenResponse:
    """Authenticate a local account.

    Every failure mode — unknown address, wrong password, inactive account, OIDC-only account —
    returns the same `401` with the same message, and the password comparison runs even when
    no user was found, so neither the body nor the timing distinguishes them (OWASP A07).

    The transaction wraps the call because a *failed* login still writes: the attempt row that
    backs rate limiting, and the `LOGIN_FAILURE` audit entry of §20.2.
    """
    with unit_of_work(db):
        tokens = service.login(email=payload.email, password=payload.password, metadata=metadata)
    return TokenResponse(
        access_token=tokens.access_token,
        refresh_token=tokens.refresh_token,
        expires_in=tokens.expires_in_seconds,
    )


@router.post(
    "/refresh",
    response_model=TokenResponse,
    summary="Exchange a refresh token for a new pair",
    responses={401: {"description": "The refresh token is invalid or has expired."}},
)
def refresh(
    payload: RefreshRequest,
    db: DbSession,
    service: Annotated[AuthService, Depends(get_auth_service)],
    metadata: Annotated[RequestMetadata, Depends(get_request_metadata)],
) -> TokenResponse:
    """Rotate a refresh token.

    The presented token is consumed and a successor issued. Replaying an already-rotated token
    revokes every session for that user: a benign retry and a stolen token look identical, so
    the safe reading is the hostile one.
    """
    with unit_of_work(db):
        tokens = service.refresh(refresh_token=payload.refresh_token, metadata=metadata)
    return TokenResponse(
        access_token=tokens.access_token,
        refresh_token=tokens.refresh_token,
        expires_in=tokens.expires_in_seconds,
    )


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke a refresh token",
)
def logout(
    payload: LogoutRequest,
    db: DbSession,
    service: Annotated[AuthService, Depends(get_auth_service)],
) -> None:
    """Revoke one refresh token.

    Returns `204` whether or not the token was live. A client that has discarded its tokens is
    logged out either way, and reporting "that token was already invalid" would be an oracle.
    """
    with unit_of_work(db):
        service.logout(refresh_token=payload.refresh_token)


@router.get(
    "/me",
    response_model=CurrentUserResponse,
    summary="The authenticated caller and their organization access",
)
def read_current_user(
    actor: CurrentUser,
    users: Annotated[UserRepository, Depends(get_user_repository)],
    access: Annotated[AccessService, Depends(get_access_service)],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> CurrentUserResponse:
    """Describe the caller and every organization they can act in.

    Permissions are resolved through the same code path every request uses, so what a client
    sees here cannot disagree with what the server will actually allow — and §5.5 keeps the
    server authoritative regardless, since UI hiding is cosmetic.
    """
    user = users.get(actor.user_id)
    if user is None:  # pragma: no cover - get_current_user already loaded this row
        raise ResourceNotFound("user")

    organizations = access.organization_access(actor.user_id)
    return CurrentUserResponse(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        employee_id=user.employee_id,
        mfa_enabled=user.mfa_enabled,
        is_platform_admin=any(
            Role.SYSTEM_SUPER_ADMIN in entry.permissions.roles for entry in organizations
        ),
        session=SessionPolicy(
            idle_timeout_seconds=settings.session_idle_timeout_seconds,
            absolute_timeout_seconds=settings.refresh_token_ttl_hours * 3600,
            access_token_ttl_seconds=settings.access_token_ttl_seconds,
        ),
        organizations=[
            OrganizationAccessSummary(
                organization_id=entry.organization_id,
                organization_code=entry.organization_code,
                display_name=entry.display_name,
                roles=sorted(role.value for role in entry.permissions.roles),
                permissions=sorted(
                    permission.value for permission in entry.permissions.permissions
                ),
                via_break_glass=entry.via_break_glass,
                expires_at=entry.expires_at,
            )
            for entry in organizations
        ],
    )
