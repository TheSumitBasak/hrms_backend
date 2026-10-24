"""Access management: user-role assignment, role-permission mapping, admin actions."""
from __future__ import annotations

import secrets
import string
import uuid

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import client_ip, get_current_user, require_permission, write_audit
from app.core.errors import ConflictError
from app.core.security import hash_password
from app.crud.base import get_or_404, jsonable
from app.models.auth import Permission, Role, RolePermission, User, UserRole
from app.models.workflow import AuditLog

router = APIRouter(tags=["Access"])


class RoleAssignment(BaseModel):
    role_id: uuid.UUID | None = None
    role_code: str | None = None


class PermissionSet(BaseModel):
    permission_ids: list[uuid.UUID] = Field(default_factory=list)


class AdminResetPasswordIn(BaseModel):
    new_password: str | None = Field(default=None, min_length=8)


class InviteIn(BaseModel):
    email: EmailStr
    employee_id: uuid.UUID | None = None
    role_codes: list[str] = Field(default_factory=list)
    send_invite: bool = True


def _gen_password(length: int = 12) -> str:
    alphabet = string.ascii_letters + string.digits + "!@#$"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _role_of(db: Session, payload: RoleAssignment) -> Role:
    if payload.role_id:
        return get_or_404(db, Role, payload.role_id, "Role")
    if payload.role_code:
        role = db.execute(select(Role).where(Role.code == payload.role_code)).scalar_one_or_none()
        if role is None:
            from app.core.errors import NotFoundError

            raise NotFoundError("Role", payload.role_code)
        return role
    from app.core.errors import APIError

    raise APIError(400, "role_id or role_code is required", "VALIDATION")


@router.get("/users/{user_id}/roles", summary="List roles assigned to a user")
def list_user_roles(
    user_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("user.view")),
) -> dict:
    user = get_or_404(db, User, user_id, "User")
    return {
        "data": [{"id": r.id, "code": r.code, "name": r.name} for r in user.roles],
        "meta": None,
        "errors": None,
    }


@router.post("/users/{user_id}/roles", status_code=201, summary="Assign a role to a user")
def assign_role(
    user_id: uuid.UUID,
    payload: RoleAssignment,
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("user.edit")),
) -> dict:
    user = get_or_404(db, User, user_id, "User")
    role = _role_of(db, payload)
    existing = db.execute(
        select(UserRole).where(UserRole.user_id == user.id, UserRole.role_id == role.id)
    ).scalar_one_or_none()
    if existing is None:
        db.add(UserRole(user_id=user.id, role_id=role.id))
        db.commit()
    return {"data": {"assigned": True}, "meta": None, "errors": None}


@router.delete("/users/{user_id}/roles/{role_id}", summary="Remove a role from a user")
def remove_role(
    user_id: uuid.UUID,
    role_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("user.edit")),
) -> dict:
    db.execute(delete(UserRole).where(UserRole.user_id == user_id, UserRole.role_id == role_id))
    db.commit()
    return {"data": {"removed": True}, "meta": None, "errors": None}


@router.get("/roles/{role_id}/permissions", summary="List permissions granted to a role")
def list_role_permissions(
    role_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("role.view")),
) -> dict:
    role = get_or_404(db, Role, role_id, "Role")
    return {
        "data": [
            {"id": p.id, "code": p.code, "module": p.module, "action": p.action}
            for p in role.permissions
        ],
        "meta": None,
        "errors": None,
    }


@router.put("/roles/{role_id}/permissions", summary="Replace permissions granted to a role")
def set_role_permissions(
    role_id: uuid.UUID,
    payload: PermissionSet,
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("role.edit")),
) -> dict:
    role = get_or_404(db, Role, role_id, "Role")
    db.execute(delete(RolePermission).where(RolePermission.role_id == role.id))
    for permission_id in set(payload.permission_ids):
        if db.get(Permission, permission_id) is not None:
            db.add(RolePermission(role_id=role.id, permission_id=permission_id))
    db.commit()
    return {"data": {"updated": len(payload.permission_ids)}, "meta": None, "errors": None}


@router.post("/users/{user_id}/reset-password", summary="Admin: reset another user's password")
def admin_reset_password(
    user_id: uuid.UUID,
    payload: AdminResetPasswordIn | None = None,
    request: Request = None,
    db: Session = Depends(get_db),
    actor: User = Depends(require_permission("user.edit")),
) -> dict:
    user = get_or_404(db, User, user_id, "User")
    temporary = payload.new_password if payload and payload.new_password else _gen_password()
    user.password_hash = hash_password(temporary)
    db.add(user)
    write_audit(
        db,
        user=actor,
        entity_type="user",
        entity_id=user.id,
        action="reset_password",
        new_values={"by_admin": str(actor.id)},
        ip_address=client_ip(request) if request else None,
    )
    db.commit()
    return {
        "data": {
            "user_id": str(user.id),
            "temporary_password": temporary,
            "must_change": payload is None or not (payload and payload.new_password),
        },
        "meta": None,
        "errors": None,
    }


@router.get("/users/{user_id}/login-history", summary="Login history for a user")
def login_history(
    user_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("user.view")),
) -> dict:
    get_or_404(db, User, user_id, "User")
    rows = db.execute(
        select(AuditLog)
        .where(AuditLog.entity_type == "auth", AuditLog.entity_id == user_id)
        .order_by(AuditLog.created_at.desc())
        .limit(100)
    ).scalars().all()
    return {
        "data": [
            {
                "action": row.action,
                "at": jsonable(row.created_at),
                "ip_address": row.ip_address,
            }
            for row in rows
        ],
        "meta": {"total": len(rows)},
        "errors": None,
    }


@router.post("/users/invite", status_code=201, summary="Invite a user (creates account + temp password)")
def invite_user(
    payload: InviteIn,
    request: Request,
    db: Session = Depends(get_db),
    actor: User = Depends(require_permission("user.create")),
) -> dict:
    exists = db.execute(select(User).where(User.email == payload.email)).scalar_one_or_none()
    if exists is not None:
        raise ConflictError("A user with this email already exists")
    temporary = _gen_password()
    user = User(
        email=payload.email,
        password_hash=hash_password(temporary),
        employee_id=payload.employee_id,
        is_active=True,
    )
    db.add(user)
    db.flush()
    for role_code in payload.role_codes:
        role = db.execute(select(Role).where(Role.code == role_code)).scalar_one_or_none()
        if role is not None:
            db.add(UserRole(user_id=user.id, role_id=role.id))
    write_audit(
        db,
        user=actor,
        entity_type="user",
        entity_id=user.id,
        action="invite",
        new_values={"email": payload.email, "send_invite": payload.send_invite},
        ip_address=client_ip(request),
    )
    db.commit()
    return {
        "data": {
            "user_id": str(user.id),
            "email": user.email,
            "temporary_password": temporary,
            "invite_sent": payload.send_invite,
        },
        "meta": None,
        "errors": None,
    }
