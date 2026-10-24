"""Reusable FastAPI dependencies: auth, RBAC, pagination and audit helpers."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from fastapi import Depends, Request
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.errors import APIError, PermissionDeniedError
from app.core.security import decode_access_token
from app.models.auth import User
from app.models.workflow import AuditLog

oauth2_scheme = OAuth2PasswordBearer(tokenUrl=f"{settings.api_prefix}/auth/login", auto_error=False)

GLOBAL_ADMIN_ROLES = {"global_admin", "super_admin", "admin"}


def get_current_user(
    token: str | None = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> User:
    if not token:
        raise APIError(401, "Authentication required", "UNAUTHENTICATED")
    payload = decode_access_token(token)
    if not payload or not payload.get("sub"):
        raise APIError(401, "Invalid or expired token", "UNAUTHENTICATED")
    user = db.get(User, _as_uuid(payload["sub"]))
    if user is None or not user.is_active:
        raise APIError(401, "User not found or inactive", "UNAUTHENTICATED")
    return user


def _as_uuid(value: Any) -> Any:
    import uuid

    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError):
        return value


def get_permission_codes(user: User) -> set[str]:
    codes: set[str] = set()
    for role in user.roles or []:
        for permission in role.permissions or []:
            codes.add(permission.code)
    return codes


def is_global_admin(user: User) -> bool:
    return any((role.code or "").lower() in GLOBAL_ADMIN_ROLES for role in user.roles or [])


def require_permission(permission: str):
    """Dependency factory enforcing a permission code (global admins bypass)."""

    def _checker(user: User = Depends(get_current_user)) -> User:
        if is_global_admin(user):
            return user
        if permission not in get_permission_codes(user):
            raise PermissionDeniedError(f"Missing permission: {permission}")
        return user

    return _checker


@dataclass
class Pagination:
    page: int = 1
    page_size: int = 25
    sort: str | None = None
    search: str | None = None
    filters: dict[str, Any] = field(default_factory=dict)

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size


def get_pagination(request: Request) -> Pagination:
    params = request.query_params
    try:
        page = max(1, int(params.get("page", 1)))
    except (TypeError, ValueError):
        page = 1
    try:
        page_size = int(params.get("pageSize", params.get("page_size", settings.default_page_size)))
    except (TypeError, ValueError):
        page_size = settings.default_page_size
    page_size = max(1, min(page_size, settings.max_page_size))

    filters: dict[str, Any] = {}
    for key, value in params.multi_items():
        if key.startswith("filter[") and key.endswith("]"):
            filters[key[len("filter[") : -1]] = value
    return Pagination(
        page=page,
        page_size=page_size,
        sort=params.get("sort"),
        search=params.get("search") or params.get("q"),
        filters=filters,
    )


def client_ip(request: Request) -> str | None:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


def write_audit(
    db: Session,
    *,
    user: User | None,
    entity_type: str,
    entity_id: Any,
    action: str,
    old_values: dict | None = None,
    new_values: dict | None = None,
    ip_address: str | None = None,
    org_id: Any | None = None,
) -> AuditLog:
    entry = AuditLog(
        org_id=org_id,
        user_id=getattr(user, "id", None),
        entity_type=entity_type,
        entity_id=_as_uuid(entity_id),
        action=action,
        old_values=old_values,
        new_values=new_values,
        ip_address=ip_address,
    )
    db.add(entry)
    return entry
