"""Platform tables: users, roles, permissions and the supporting token/idempotency stores.

These are not organization-scoped (§23.2 "Platform"): a user exists once and is granted access
to organizations through ``user_organization_access`` (:mod:`app.models.access`).

Three tables here are documented additions the specification requires behaviour from without
defining storage for: ``refresh_tokens`` (DEC-005), ``idempotency_keys`` (DEC-006) and
``organization_role_permissions`` (DEC-004, in :mod:`app.models.organization`).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.mixins import (
    AppendOnlyColumns,
    PrimaryKeyColumn,
    StandardColumns,
    archive_reason_constraint,
)
from app.db.types import Uuid7

if TYPE_CHECKING:
    from app.models.access import UserOrganizationAccess

EMAIL_MAX_LENGTH = 254
"""§21.1 V-EMAIL."""


class UserStatus(StrEnum):
    """§23.2: users are deactivated, never deleted."""

    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"


class AuthProvider(StrEnum):
    """How a user authenticates.

    ``LOCAL`` accounts carry a password hash; ``OIDC`` accounts never do, so a local password
    cannot be set on an account that is meant to authenticate through the identity provider
    (§24.1, A-04).
    """

    LOCAL = "LOCAL"
    OIDC = "OIDC"


class User(Base, StandardColumns):
    """A platform user account (§23.2).

    ``email`` is stored lower-cased and trimmed by the service (§21.1 V-EMAIL), so the plain
    unique constraint is effectively case-insensitive without depending on the server's
    collation.

    ``employee_id`` optionally links the account to one employment record, which is what the
    §5.4 Department Manager scope and the §5.6 self-view resolve against. It is deliberately
    **nullable and ``SET NULL`` on delete** per §22, and the foreign key is added by a separate
    ``ALTER TABLE`` (``use_alter``) because ``employees`` carries ``created_by`` back to this
    table — a genuine cycle that cannot be expressed with inline constraints.
    """

    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("email", name="uq_users_email"),
        CheckConstraint(
            f"status IN ('{UserStatus.ACTIVE}','{UserStatus.INACTIVE}')", name="ck_users_status"
        ),
        CheckConstraint(
            f"auth_provider IN ('{AuthProvider.LOCAL}','{AuthProvider.OIDC}')",
            name="ck_users_auth_provider",
        ),
        CheckConstraint(
            "(auth_provider = 'LOCAL') OR (password_hash IS NULL)",
            name="ck_users_password_only_for_local",
        ),
        archive_reason_constraint("users"),
        Index("ix_users_status", "status"),
        Index("ix_users_employee_id", "employee_id"),
    )

    email: Mapped[str] = mapped_column(String(EMAIL_MAX_LENGTH), nullable=False)
    display_name: Mapped[str] = mapped_column(String(150), nullable=False)
    employee_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid7,
        ForeignKey("employees.id", ondelete="SET NULL", use_alter=True),
        nullable=True,
    )
    auth_provider: Mapped[str] = mapped_column(
        String(10), nullable=False, default=AuthProvider.OIDC
    )
    password_hash: Mapped[str | None] = mapped_column(String(100), nullable=True)
    """bcrypt hash, present only for ``LOCAL`` accounts. Never returned by any endpoint and
    never recorded in an audit payload (§20.3)."""

    mfa_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    """NFR-12. Enforced at the identity provider; recorded here for the §25.5 access review."""

    status: Mapped[str] = mapped_column(String(10), nullable=False, default=UserStatus.ACTIVE)
    last_login_at: Mapped[datetime | None] = mapped_column(nullable=True)

    access_grants: Mapped[list[UserOrganizationAccess]] = relationship(
        back_populates="user",
        foreign_keys="UserOrganizationAccess.user_id",
        lazy="raise",
    )
    """``lazy="raise"`` on purpose: an accidental lazy load inside a serialisation loop is the
    classic N+1 (§17 of the brief). Callers must eager-load explicitly."""

    @property
    def is_active(self) -> bool:
        return self.status == UserStatus.ACTIVE and self.archived_at is None


class Role(Base, PrimaryKeyColumn):
    """A role definition (§23.2).

    The §5.1 roles are seeded as system roles. ``is_system`` protects them from deletion, and
    §24.4.8 makes the SSA role's permission set immutable even to an SSA.
    """

    __tablename__ = "roles"
    __table_args__ = (
        UniqueConstraint("role_code", name="uq_roles_role_code"),
        CheckConstraint(
            "CHAR_LENGTH(role_code) BETWEEN 2 AND 20", name="ck_roles_role_code_length"
        ),
    )

    role_code: Mapped[str] = mapped_column(String(20), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    is_system: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    created_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=text("CURRENT_TIMESTAMP(6)")
    )
    updated_at: Mapped[datetime] = mapped_column(
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP(6)"),
        server_onupdate=text("CURRENT_TIMESTAMP(6)"),
    )

    permissions: Mapped[list[Permission]] = relationship(
        secondary="role_permissions", lazy="selectin"
    )
    """Eager via ``selectin``: resolving a caller's permissions needs these on every request,
    so a separate round trip per role would be the N+1 this design is meant to avoid."""


class Permission(Base, PrimaryKeyColumn):
    """A permission code (§23.2, static).

    Rows mirror :class:`app.core.permissions.Permission`; the enum is the source of truth and
    the table exists so role grants can reference them and so §24.4.8 can list the catalogue.
    """

    __tablename__ = "permissions"
    __table_args__ = (UniqueConstraint("permission_code", name="uq_permissions_permission_code"),)

    permission_code: Mapped[str] = mapped_column(String(60), nullable=False)
    description: Mapped[str] = mapped_column(String(300), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=text("CURRENT_TIMESTAMP(6)")
    )


class RolePermission(Base):
    """Role ↔ permission link (§22: "CASCADE (link rows)").

    A pure link table, so it has a composite primary key and no standard columns. Changes are
    audited as ``ROLE_PERMISSIONS_CHANGED`` against the role.
    """

    __tablename__ = "role_permissions"

    role_id: Mapped[uuid.UUID] = mapped_column(
        Uuid7, ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True
    )
    permission_id: Mapped[uuid.UUID] = mapped_column(
        Uuid7, ForeignKey("permissions.id", ondelete="CASCADE"), primary_key=True
    )


class RefreshToken(Base, PrimaryKeyColumn):
    """An issued refresh token (DEC-005).

    Only a SHA-256 digest of the token is stored, never the token itself: a database disclosure
    must not yield usable credentials. Rotation is modelled by ``replaced_by_id``, so a replayed
    old token is detectable rather than merely expired.

    §25.5 requires user deactivation to revoke refresh tokens **immediately**, which is a single
    UPDATE over this table rather than anything token-format dependent.
    """

    __tablename__ = "refresh_tokens"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_refresh_tokens_token_hash"),
        CheckConstraint("expires_at > created_at", name="ck_refresh_tokens_expiry_after_issue"),
        Index("ix_refresh_tokens_user_id_revoked_at", "user_id", "revoked_at"),
        Index("ix_refresh_tokens_expires_at", "expires_at"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid7, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=text("CURRENT_TIMESTAMP(6)")
    )
    revoked_at: Mapped[datetime | None] = mapped_column(nullable=True)
    revoked_reason: Mapped[str | None] = mapped_column(String(100), nullable=True)
    replaced_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid7, ForeignKey("refresh_tokens.id", ondelete="SET NULL"), nullable=True
    )
    user_agent: Mapped[str | None] = mapped_column(String(300), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)


class IdempotencyKey(Base, PrimaryKeyColumn):
    """A recorded idempotent request (DEC-006).

    §24.1 requires an ``Idempotency-Key`` on the create endpoints for employees, salary
    assignments and documents, with a replay inside 24 hours returning the original response,
    and §24.5 requires ``409 IDEMPOTENCY_KEY_REUSED`` when the same key arrives with a
    different body. Both need the key, a fingerprint of the request and the stored response.

    ``request_fingerprint`` is a SHA-256 of the canonical request body, not the body itself:
    a create-employee body is full of personal data and this table is not the place for a
    second copy of it.

    The unique constraint spans ``(user_id, idempotency_key)`` so two users cannot collide on a
    guessable key, and one user cannot have a key interpreted differently per endpoint.
    """

    __tablename__ = "idempotency_keys"
    __table_args__ = (
        UniqueConstraint("user_id", "idempotency_key", name="uq_idempotency_keys_user_key"),
        CheckConstraint("expires_at > created_at", name="ck_idempotency_keys_expiry"),
        Index("ix_idempotency_keys_expires_at", "expires_at"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid7, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    idempotency_key: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    request_method: Mapped[str] = mapped_column(String(10), nullable=False)
    request_path: Mapped[str] = mapped_column(String(300), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    response_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    """The original response, replayed verbatim. Null while the first request is still in
    flight, which is how a concurrent duplicate is distinguished from a completed one."""

    created_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=text("CURRENT_TIMESTAMP(6)")
    )
    completed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    expires_at: Mapped[datetime] = mapped_column(nullable=False)


class LoginAttempt(Base, AppendOnlyColumns):
    """Append-only record of authentication attempts.

    §20.2 audits ``LOGIN_SUCCESS`` and ``LOGIN_FAILURE``, but those rows live in
    ``audit_logs``, which requires an ``actor_user_id``. A failed login against an unknown
    email has no actor, so it cannot be represented there. This table captures those attempts
    for the rate limiting and lockout §25.9 asks for, keyed by a **hash** of the submitted
    email so the table does not become a list of guessed addresses.
    """

    __tablename__ = "login_attempts"
    __table_args__ = (
        Index("ix_login_attempts_email_hash_created_at", "email_hash", "created_at"),
        Index("ix_login_attempts_ip_address_created_at", "ip_address", "created_at"),
    )

    email_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid7, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    succeeded: Mapped[bool] = mapped_column(Boolean, nullable=False)
    failure_reason: Mapped[str | None] = mapped_column(String(60), nullable=True)
    """A stable code such as ``NO_SUCH_USER`` or ``BAD_PASSWORD``, for operational analysis.
    It is never returned to a client, which sees only a generic failure (OWASP A07)."""

    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(300), nullable=True)
