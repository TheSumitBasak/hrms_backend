"""Who is acting, and in which organization (§5, §6.3).

Two immutable value objects flow from the API layer down through services into repositories:

:class:`ActorContext`
    The authenticated caller, independent of any organization. Carries correlation metadata so
    an audit row can record the request without a service reaching for request state.

:class:`OrgContext`
    An actor **plus one organization** plus the permissions they hold *there*. Produced only by
    the API dependency that has already checked the caller holds a grant for the organization
    in the path, so a service receiving one can treat the organization as authorized.

Both are frozen. A context is passed into serialisers and repositories, and a mutable one
would make it possible to widen permissions halfway through a request — the kind of bug that
is invisible in review and catastrophic in effect.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime

from app.core.errors import PermissionDenied
from app.core.permissions import (
    DM_MAX_REPORTING_DEPTH,
    Permission,
    PermissionSet,
    Role,
)

SYSTEM_ACTOR_NAME = "system"
"""§20.1: scheduled jobs write audit rows as a system user rather than as nobody."""


@dataclass(frozen=True, slots=True)
class RequestMetadata:
    """Correlation and provenance for the §20.1 audit columns.

    ``ip_address`` is personal data (§20.1 says so explicitly) and is subject to the same
    retention schedule as the rest of the audit row.
    """

    request_id: str | None = None
    ip_address: str | None = None
    user_agent: str | None = None

    @classmethod
    def empty(cls) -> RequestMetadata:
        return cls()


@dataclass(frozen=True, slots=True)
class ActorContext:
    """An authenticated caller, before any organization is chosen."""

    user_id: uuid.UUID
    email: str
    display_name: str
    employee_id: uuid.UUID | None = None
    """The employment record this account is linked to, if any. Drives the §5.4 Department
    Manager scope and the §5.6 self-view."""

    is_platform_admin: bool = False
    """Whether the caller holds the System Super Admin role. Note this grants **no** employee
    data access (§5.3 note 1); it gates the platform endpoints only."""

    metadata: RequestMetadata = field(default_factory=RequestMetadata.empty)

    def for_organization(
        self,
        organization_id: uuid.UUID,
        *,
        permissions: PermissionSet,
        via_break_glass: bool = False,
    ) -> OrgContext:
        """Narrow this actor to one organization with the permissions they hold there."""
        return OrgContext(
            user_id=self.user_id,
            email=self.email,
            display_name=self.display_name,
            employee_id=self.employee_id,
            is_platform_admin=self.is_platform_admin,
            metadata=self.metadata,
            organization_id=organization_id,
            permissions=permissions,
            via_break_glass=via_break_glass,
        )


@dataclass(frozen=True, slots=True)
class OrgContext(ActorContext):
    """An actor acting inside one authorized organization.

    Constructing one asserts that the caller holds an active grant for ``organization_id``;
    that check belongs to :mod:`app.api.deps` and is not repeated here. What *is* repeated
    everywhere downstream is ``organization_id``, because every organization-scoped query is
    filtered by it (§6.1 layer 3).
    """

    organization_id: uuid.UUID = field(kw_only=True)
    permissions: PermissionSet = field(kw_only=True)
    via_break_glass: bool = field(default=False, kw_only=True)
    """True when access comes from a time-bound §5.3 break-glass grant. Every access made
    under one is audited, so services check this to emit ``BREAK_GLASS_ACCESS``."""

    # ---------------------------------------------------------------- permissions

    def has(self, permission: Permission) -> bool:
        return self.permissions.has(permission)

    def has_any(self, *permissions: Permission) -> bool:
        return self.permissions.has_any(*permissions)

    def require(self, permission: Permission) -> None:
        """Raise ``403 PERMISSION_DENIED`` unless the caller holds ``permission``.

        Services call this rather than returning a boolean, so a forgotten check is a missing
        statement rather than an ignored return value.
        """
        if not self.permissions.has(permission):
            raise PermissionDenied(f"This action requires the '{permission.value}' permission.")

    def require_any(self, *permissions: Permission) -> None:
        if not self.permissions.has_any(*permissions):
            codes = ", ".join(permission.value for permission in permissions)
            raise PermissionDenied(f"This action requires one of: {codes}.")

    @property
    def roles(self) -> frozenset[Role]:
        return self.permissions.roles

    # ---------------------------------------------------------------- scoping

    @property
    def is_reporting_scoped(self) -> bool:
        """§5.4: whether employee visibility is limited to the caller's reporting tree."""
        return self.permissions.is_reporting_scoped

    @property
    def reporting_scope_depth(self) -> int:
        return DM_MAX_REPORTING_DEPTH

    @property
    def employee_field_allowlist(self) -> frozenset[str] | None:
        """The §5.4 field set for this caller, or ``None`` for no allowlist restriction."""
        return self.permissions.employee_field_allowlist

    def is_self(self, employee_id: uuid.UUID) -> bool:
        """Whether ``employee_id`` is this caller's own record (§5.6)."""
        return self.employee_id is not None and self.employee_id == employee_id


@dataclass(frozen=True, slots=True)
class SystemContext:
    """The context a scheduled job acts under (§24.7).

    Jobs are not users and hold no organization grants, so they do not get an
    :class:`OrgContext`. They carry a dedicated system user id purely so the audit rows they
    write have an actor, which §20.1 requires. A job must still scope its own queries by
    organization explicitly — this type grants nothing.
    """

    user_id: uuid.UUID
    job_name: str
    started_at: datetime
    request_id: str | None = None

    @property
    def metadata(self) -> RequestMetadata:
        return RequestMetadata(request_id=self.request_id)


__all__ = [
    "SYSTEM_ACTOR_NAME",
    "ActorContext",
    "OrgContext",
    "RequestMetadata",
    "SystemContext",
]
