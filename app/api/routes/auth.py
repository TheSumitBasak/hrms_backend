"""Authentication: login, refresh, registration, password reset, self-service."""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Cookie, Depends, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.deps import (
    client_ip,
    get_current_user,
    get_permission_codes,
    is_global_admin,
    write_audit,
)
from app.core.errors import APIError, ConflictError, NotFoundError
from app.core.security import (
    create_access_token,
    create_refresh_token,
    create_reset_token,
    decode_token,
    hash_password,
    verify_password,
)
from app.models.auth import Role, User, UserRole
from app.models.employee import Employee
from app.models.organization import Organization
from app.models.user_pref import UserPreference
from app.schemas.common import (
    ChangePasswordRequest,
    ForgotPasswordRequest,
    LoginRequest,
    RefreshRequest,
    RegisterRequest,
    ResetPasswordRequest,
    SwitchOrgRequest,
    Token,
    UpdateMeRequest,
    UserRead,
)

router = APIRouter(prefix="/auth", tags=["Auth"])


def _user_payload(user: User) -> UserRead:
    return UserRead(
        id=user.id,
        email=user.email,
        employee_id=user.employee_id,
        is_active=user.is_active,
        role_codes=[role.code for role in user.roles or []],
        permissions=sorted(get_permission_codes(user)),
        is_global_admin=is_global_admin(user),
    )


def _issue_tokens(user: User, response: Response | None = None) -> Token:
    access = create_access_token(str(user.id))
    refresh = create_refresh_token(str(user.id))
    if response is not None:
        response.set_cookie(
            "refresh_token",
            refresh,
            httponly=True,
            samesite="lax",
            secure=False,
            max_age=settings.refresh_token_expire_days * 24 * 3600,
            path="/",
        )
    return Token(
        access_token=access,
        refresh_token=refresh,
        expires_in=settings.access_token_expire_minutes * 60,
    )


def _get_or_create_prefs(db: Session, user_id: uuid.UUID) -> UserPreference:
    prefs = db.execute(
        select(UserPreference).where(UserPreference.user_id == user_id)
    ).scalar_one_or_none()
    if prefs is None:
        prefs = UserPreference(user_id=user_id)
        db.add(prefs)
        db.flush()
    return prefs


@router.post("/login", summary="Login and obtain access + refresh tokens")
def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> dict:
    user = db.execute(select(User).where(User.email == payload.email)).scalar_one_or_none()
    if user is None or not verify_password(payload.password, user.password_hash):
        raise APIError(401, "Invalid email or password", "UNAUTHENTICATED")
    if not user.is_active:
        raise APIError(403, "Account is disabled", "FORBIDDEN")
    user.last_login_at = datetime.now(timezone.utc)
    db.add(user)
    write_audit(
        db,
        user=user,
        entity_type="auth",
        entity_id=user.id,
        action="login",
        ip_address=client_ip(request),
    )
    db.commit()
    token = _issue_tokens(user, response)
    return {"data": token.model_dump(), "meta": None, "errors": None}


@router.post("/refresh", summary="Exchange a refresh token for a new access token")
def refresh(
    payload: RefreshRequest | None = None,
    response: Response = None,
    refresh_token: str | None = Cookie(None),
    db: Session = Depends(get_db),
) -> dict:
    token = (payload.refresh_token if payload else None) or refresh_token
    if not token:
        raise APIError(401, "Refresh token required", "UNAUTHENTICATED")
    data = decode_token(token)
    if data is None or data.get("type") != "refresh":
        raise APIError(401, "Invalid or expired refresh token", "UNAUTHENTICATED")
    try:
        user_id = uuid.UUID(str(data.get("sub")))
    except (ValueError, TypeError):
        raise APIError(401, "Invalid refresh token subject", "UNAUTHENTICATED")
    user = db.get(User, user_id)
    if user is None or not user.is_active:
        raise APIError(401, "User not found or inactive", "UNAUTHENTICATED")
    new_token = _issue_tokens(user, response)
    return {"data": new_token.model_dump(), "meta": None, "errors": None}


@router.post("/register", status_code=201, summary="Register a user")
def register(
    payload: RegisterRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    exists = db.execute(select(User).where(User.email == payload.email)).scalar_one_or_none()
    if exists is not None:
        raise ConflictError("A user with this email already exists")
    user = User(
        email=payload.email,
        password_hash=hash_password(payload.password),
        employee_id=uuid.UUID(payload.employee_id) if payload.employee_id else None,
    )
    db.add(user)
    db.flush()
    for role_code in payload.role_codes:
        role = db.execute(select(Role).where(Role.code == role_code)).scalar_one_or_none()
        if role is not None:
            db.add(UserRole(user_id=user.id, role_id=role.id))
    write_audit(
        db,
        user=user,
        entity_type="user",
        entity_id=user.id,
        action="create",
        ip_address=client_ip(request),
    )
    db.commit()
    db.refresh(user)
    return {"data": _user_payload(user).model_dump(mode="json"), "meta": None, "errors": None}


@router.post("/forgot-password", summary="Request a password reset token")
def forgot_password(
    payload: ForgotPasswordRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    user = db.execute(select(User).where(User.email == payload.email)).scalar_one_or_none()
    response_data: dict = {"message": "If the email exists, a reset link has been sent."}
    if user is not None:
        token = create_reset_token(str(user.id), user.password_hash)
        write_audit(
            db,
            user=user,
            entity_type="auth",
            entity_id=user.id,
            action="forgot_password",
            ip_address=client_ip(request),
        )
        db.commit()
        # Email delivery is out of scope; expose the token in debug only.
        if settings.debug:
            response_data["reset_token"] = token
    return {"data": response_data, "meta": None, "errors": None}


@router.post("/reset-password", summary="Consume a reset token and set a new password")
def reset_password(
    payload: ResetPasswordRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    data = decode_token(payload.token)
    if data is None or data.get("type") != "reset":
        raise APIError(400, "Invalid or expired reset token", "VALIDATION")
    try:
        user_id = uuid.UUID(str(data.get("sub")))
    except (ValueError, TypeError):
        raise APIError(400, "Invalid reset token subject", "VALIDATION")
    user = db.get(User, user_id)
    if user is None:
        raise NotFoundError("User")
    fingerprint = hashlib.sha256(user.password_hash.encode("utf-8")).hexdigest()[:16]
    if fingerprint != data.get("fp"):
        raise APIError(400, "Reset token has already been used", "VALIDATION")
    user.password_hash = hash_password(payload.new_password)
    db.add(user)
    write_audit(
        db,
        user=user,
        entity_type="auth",
        entity_id=user.id,
        action="reset_password",
        ip_address=client_ip(request),
    )
    db.commit()
    return {"data": {"updated": True}, "meta": None, "errors": None}


@router.get("/me", summary="Current authenticated user")
def me(user: User = Depends(get_current_user)) -> dict:
    return {"data": _user_payload(user).model_dump(mode="json"), "meta": None, "errors": None}


@router.patch("/me", summary="Update own account and profile fields")
def update_me(
    payload: UpdateMeRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    data = payload.model_dump(exclude_unset=True)
    if "email" in data and data["email"] and data["email"] != user.email:
        clash = db.execute(
            select(User).where(User.email == data["email"], User.id != user.id)
        ).scalar_one_or_none()
        if clash is not None:
            raise ConflictError("Email already in use")
        user.email = data.pop("email")
    else:
        data.pop("email", None)

    prefs = _get_or_create_prefs(db, user.id)
    for key in ("locale", "theme"):
        if key in data:
            setattr(prefs, key, data.pop(key))

    if user.employee_id:
        employee = db.get(Employee, user.employee_id)
        if employee is not None:
            if "first_name" in data:
                employee.first_name = data["first_name"]
            if "last_name" in data:
                employee.last_name = data["last_name"]
            if "phone" in data:
                employee.phone = data["phone"]
            if "avatar_url" in data:
                employee.avatar_path = data["avatar_url"]
            db.add(employee)

    db.add(user)
    db.add(prefs)
    write_audit(
        db,
        user=user,
        entity_type="user",
        entity_id=user.id,
        action="update",
        new_values=payload.model_dump(exclude_unset=True),
        ip_address=client_ip(request),
    )
    db.commit()
    db.refresh(user)
    return {"data": _user_payload(user).model_dump(mode="json"), "meta": None, "errors": None}


@router.get("/permissions", summary="Effective permissions (roles + implicit roles)")
def permissions(user: User = Depends(get_current_user)) -> dict:
    codes = sorted(get_permission_codes(user))
    global_admin = is_global_admin(user)
    return {
        "data": {
            "permissions": codes,
            "roles": [role.code for role in user.roles or []],
            "is_global_admin": global_admin,
            "effective": ["*"] if global_admin else codes,
        },
        "meta": None,
        "errors": None,
    }


@router.post("/switch-org", summary="Set the active organisation for this session/user")
def switch_org(
    payload: SwitchOrgRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    try:
        org_id = uuid.UUID(payload.org_id)
    except (ValueError, TypeError):
        raise APIError(400, "org_id must be a valid UUID", "VALIDATION")
    org = db.get(Organization, org_id)
    if org is None:
        raise NotFoundError("Organization", payload.org_id)
    prefs = _get_or_create_prefs(db, user.id)
    prefs.active_org_id = org.id
    write_audit(
        db,
        user=user,
        entity_type="user_preferences",
        entity_id=user.id,
        action="update",
        new_values={"active_org_id": str(org.id)},
    )
    db.commit()
    return {
        "data": {"active_org_id": str(org.id), "name": org.name, "default_currency": org.default_currency},
        "meta": None,
        "errors": None,
    }


@router.post("/change-password", summary="Change own password")
def change_password(
    payload: ChangePasswordRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    if not verify_password(payload.current_password, user.password_hash):
        raise APIError(400, "Current password is incorrect", "VALIDATION")
    user.password_hash = hash_password(payload.new_password)
    db.add(user)
    db.commit()
    return {"data": {"updated": True}, "meta": None, "errors": None}


@router.post("/logout", summary="Logout (clears the refresh cookie)")
def logout(response: Response, user: User = Depends(get_current_user)) -> dict:
    response.delete_cookie("refresh_token", path="/")
    return {"data": {"logged_out": True}, "meta": None, "errors": None}
