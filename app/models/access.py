"""Organization access grants and per-organization permission overrides (§5, §23.2).

``user_organization_access`` is where authorization actually lives. A user has no access to
any organization until a row here says otherwise, and §5.1 is explicit that there is **no
implicit inheritance** — a "group-level grant" is simply several per-organization rows created
together.

Break-glass (§5.3 note 1) is not a separate mechanism: it is an ordinary grant with
``expires_at`` set and a mandatory reason (DEC-007). Every downstream permission check and
audit entry therefore works unchanged, and only the grant's provenance differs.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Computed,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.mixins import PrimaryKeyColumn
from app.db.types import Uuid7

if TYPE_CHECKING:
    from app.models.organization import Organization
    from app.models.platform import Role, User

GRANT_REASON_MIN_LENGTH = 20
"""§5.3 note 1: a break-glass reason must be at least twenty characters."""


class UserOrganizationAccess(Base, PrimaryKeyColumn):
    """One role granted to one user in one organization (§23.2).

    Revocation is **soft**: ``revoked_at`` is set and the row is retained ("Revoked rows
    retained", §23.2), so the audit trail can show who held what and when. Every query for
    effective access therefore filters on ``revoked_at IS NULL`` *and* an unexpired
    ``expires_at``.

    The unique constraint uses a generated column that is ``NULL`` once the grant is revoked,
    which reproduces the §23.3 ``partial unique (user_id, organization_id, role_id) where
    revoked_at is null``: a user may hold the same role again after revocation, but never twice
    at once (ADR-003).
    """

    __tablename__ = "user_organization_access"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "organization_id",
            "role_id",
            "active_grant_key",
            name="uq_user_organization_access_active_grant",
        ),
        CheckConstraint(
            "expires_at IS NULL OR expires_at > granted_at",
            name="ck_user_organization_access_expiry_after_grant",
        ),
        CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= granted_at",
            name="ck_user_organization_access_revoked_after_grant",
        ),
        # §5.3 note 1: a time-bound (break-glass) grant must carry a reason. A permanent grant
        # may also carry one, so the rule is one-directional.
        CheckConstraint(
            "is_break_glass = 0 OR ("
            "expires_at IS NOT NULL AND reason IS NOT NULL "
            f"AND CHAR_LENGTH(reason) >= {GRANT_REASON_MIN_LENGTH}"
            ")",
            name="ck_user_organization_access_break_glass_requires_reason",
        ),
        Index("ix_user_organization_access_user_id", "user_id"),
        Index("ix_user_organization_access_organization_id", "organization_id"),
        # The hot path: "which active grants does this user hold in this organization?"
        Index(
            "ix_user_org_access_lookup",
            "user_id",
            "organization_id",
            "revoked_at",
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid7, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid7, ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    role_id: Mapped[uuid.UUID] = mapped_column(
        Uuid7, ForeignKey("roles.id", ondelete="RESTRICT"), nullable=False
    )

    granted_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid7, ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    granted_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=text("CURRENT_TIMESTAMP(6)")
    )
    expires_at: Mapped[datetime | None] = mapped_column(nullable=True)
    """Null means permanent. Mandatory for a break-glass grant, capped at four hours (§5.3)."""

    revoked_at: Mapped[datetime | None] = mapped_column(nullable=True)
    revoked_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid7, ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    reason: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    is_break_glass: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )

    active_grant_key: Mapped[int | None] = mapped_column(
        Computed("IF(revoked_at IS NULL, 1, NULL)", persisted=True), nullable=True
    )

    user: Mapped[User] = relationship(
        back_populates="access_grants", foreign_keys=[user_id], lazy="raise"
    )
    organization: Mapped[Organization] = relationship(lazy="raise")
    role: Mapped[Role] = relationship(lazy="selectin")
    """Eager: resolving permissions needs the role and its permission set on every request."""

    def is_effective_at(self, moment: datetime) -> bool:
        """Whether this grant confers access at ``moment``.

        A grant is effective when it has not been revoked, has started, and has not expired.
        ``expires_at`` is treated as exclusive, consistent with every other interval in the
        system (§1.4).
        """
        if self.revoked_at is not None:
            return False
        if moment < self.granted_at:
            return False
        return self.expires_at is None or moment < self.expires_at


class OrganizationRolePermission(Base, PrimaryKeyColumn):
    """A ``C`` cell of the §5.3 matrix, switched on for one organization (DEC-004).

    §5.3 marks fourteen capabilities "not granted by default, may be enabled per organization
    by an OA" — HR Executive salary access, Restricted Document Reviewer reveal, HR Admin
    master management, and so on. Storing them keeps that configuration rather than code
    (NFR-15).

    A row here is only ever *consulted*: :func:`app.core.permissions.resolve_permissions`
    intersects it with the role's declared ``C`` set, so a stale or hostile row cannot grant a
    permission that is not a ``C`` cell for that role. That property is asserted by
    ``tests/unit/test_permissions.py``.
    """

    __tablename__ = "organization_role_permissions"
    __table_args__ = (
        UniqueConstraint(
            "organization_id", "role_code", "permission_code", name="uq_org_role_permission"
        ),
        UniqueConstraint("organization_id", "id", name="uq_organization_role_permissions_org_id"),
        Index("ix_org_role_permissions_lookup", "organization_id", "role_code"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid7, ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    role_code: Mapped[str] = mapped_column(String(20), nullable=False)
    permission_code: Mapped[str] = mapped_column(String(60), nullable=False)
    """Stored as the code rather than a FK to ``permissions``, because the authoritative list
    is :class:`app.core.permissions.Permission`; the row is validated against that enum and
    against the role's ``C`` set before being written."""

    enabled_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid7, ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    enabled_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=text("CURRENT_TIMESTAMP(6)")
    )
