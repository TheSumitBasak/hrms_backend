"""Resolving what a caller may do, and granting or revoking that (§5).

:class:`AccessService` answers one question on every request — "what permissions does this user
hold in this organization?" — and owns the grant lifecycle behind it.

The answer is cached for at most :attr:`Settings.permission_cache_ttl_seconds` (default 60),
which is the ceiling §5.5 sets: *"Permission changes take effect within 60 seconds (permission
cache TTL ≤ 60 s)"*. The cache is keyed on ``(user, organization)`` and is invalidated eagerly
whenever a grant in that pair changes, so the TTL is a worst case for changes made on another
worker, not the normal latency. Nothing else is cached: §18 of the brief forbids caching
sensitive data or permission checks without correct invalidation, and this is the one place
with both.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.audit.actions import AuditAction
from app.audit.events import AuditEvent, format_actor_roles
from app.audit.writer import AuditWriter
from app.core.clock import utc_now
from app.core.context import ActorContext
from app.core.errors import (
    OrgAccessDenied,
    PermissionDenied,
    ResourceNotFound,
    ValidationFailed,
)
from app.core.permissions import (
    BREAK_GLASS_MAX_MINUTES,
    BREAK_GLASS_MIN_REASON_LENGTH,
    BREAK_GLASS_ROLE,
    ORG_ADMIN_GRANTABLE_ROLES,
    Permission,
    PermissionSet,
    Role,
    configurable_permissions_for,
    resolve_permissions,
)
from app.models.access import UserOrganizationAccess
from app.models.platform import User
from app.repositories.organization import OrganizationRepository
from app.repositories.platform import (
    AccessGrantRepository,
    OrganizationRolePermissionRepository,
    RefreshTokenRepository,
    UserRepository,
)


@dataclass(frozen=True, slots=True)
class _CacheEntry:
    permissions: PermissionSet
    via_break_glass: bool
    expires_at: datetime


class PermissionCache:
    """A small, bounded, TTL cache of resolved permission sets (§5.5).

    Process-local and lock-guarded, because endpoints are served from a threadpool (ADR-001).
    Deliberately *not* shared across workers: a shared cache would need distributed
    invalidation, and getting that wrong means a revoked grant staying live. A per-process
    cache with a 60-second ceiling has a bounded, documented worst case instead.
    """

    __slots__ = ("_entries", "_lock", "_max_entries", "_ttl")

    def __init__(self, *, ttl_seconds: int, max_entries: int = 4096) -> None:
        self._ttl = timedelta(seconds=ttl_seconds)
        self._entries: dict[tuple[uuid.UUID, uuid.UUID], _CacheEntry] = {}
        self._lock = threading.Lock()
        self._max_entries = max_entries

    def get(
        self, user_id: uuid.UUID, organization_id: uuid.UUID, *, now: datetime | None = None
    ) -> _CacheEntry | None:
        if self._ttl.total_seconds() <= 0:
            return None
        moment = now or utc_now()
        with self._lock:
            entry = self._entries.get((user_id, organization_id))
            if entry is None:
                return None
            if entry.expires_at <= moment:
                del self._entries[(user_id, organization_id)]
                return None
            return entry

    def put(
        self,
        user_id: uuid.UUID,
        organization_id: uuid.UUID,
        permissions: PermissionSet,
        *,
        via_break_glass: bool,
        now: datetime | None = None,
    ) -> None:
        if self._ttl.total_seconds() <= 0:
            return
        moment = now or utc_now()
        with self._lock:
            if len(self._entries) >= self._max_entries:
                # Simple bound rather than an LRU: entries expire within a minute anyway, so
                # eviction policy cannot matter much, and an unbounded dict could not be
                # defended at the 500-organization scale of NFR-17.
                self._entries.clear()
            self._entries[(user_id, organization_id)] = _CacheEntry(
                permissions=permissions,
                via_break_glass=via_break_glass,
                expires_at=moment + self._ttl,
            )

    def invalidate_user(self, user_id: uuid.UUID) -> None:
        """Drop every cached entry for a user, on deactivation or a grant change."""
        with self._lock:
            for key in [key for key in self._entries if key[0] == user_id]:
                del self._entries[key]

    def invalidate(self, user_id: uuid.UUID, organization_id: uuid.UUID) -> None:
        with self._lock:
            self._entries.pop((user_id, organization_id), None)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


@dataclass(frozen=True, slots=True)
class ResolvedAccess:
    """The outcome of resolving a caller's access to one organization."""

    permissions: PermissionSet
    via_break_glass: bool


@dataclass(frozen=True, slots=True)
class OrganizationAccess:
    """One organization a caller can act in, with the access they hold there.

    Assembled by the service rather than the endpoint, so the organization lookup is a single
    batched query and the handler needs no repository of its own.
    """

    organization_id: uuid.UUID
    organization_code: str
    display_name: str
    permissions: PermissionSet
    via_break_glass: bool
    expires_at: datetime | None


class AccessService:
    """Permission resolution and the organization-grant lifecycle (§5, §24.4.8)."""

    def __init__(
        self,
        *,
        users: UserRepository,
        grants: AccessGrantRepository,
        overrides: OrganizationRolePermissionRepository,
        refresh_tokens: RefreshTokenRepository,
        organizations: OrganizationRepository,
        audit: AuditWriter,
        cache: PermissionCache,
    ) -> None:
        self._users = users
        self._grants = grants
        self._overrides = overrides
        self._refresh_tokens = refresh_tokens
        self._organizations = organizations
        self._audit = audit
        self._cache = cache

    # ---------------------------------------------------------------- resolution

    def resolve(
        self,
        user_id: uuid.UUID,
        organization_id: uuid.UUID,
        *,
        now: datetime | None = None,
    ) -> ResolvedAccess:
        """What ``user_id`` may do in ``organization_id``.

        Raises ``403 ORG_ACCESS_DENIED`` when the user holds no active grant there — §24.3
        distinguishes that from ``PERMISSION_DENIED``, which means "you have access to this
        organization but not this capability".
        """
        cached = self._cache.get(user_id, organization_id, now=now)
        if cached is not None:
            return ResolvedAccess(
                permissions=cached.permissions, via_break_glass=cached.via_break_glass
            )

        active = self._grants.active_grants_in_organization(user_id, organization_id, at=now)
        if not active:
            raise OrgAccessDenied()

        roles: set[Role] = set()
        via_break_glass = False
        for grant in active:
            try:
                roles.add(Role(grant.role.role_code))
            except ValueError:
                # A role code with no matching enum member is configuration drift. Ignoring it
                # fails closed rather than inventing a permission set for it.
                continue
            if grant.is_break_glass:
                via_break_glass = True

        enabled = self._enabled_configurable(organization_id, roles)
        permissions = resolve_permissions(frozenset(roles), enabled_configurable=enabled)
        self._cache.put(
            user_id, organization_id, permissions, via_break_glass=via_break_glass, now=now
        )
        return ResolvedAccess(permissions=permissions, via_break_glass=via_break_glass)

    def accessible_organization_ids(
        self, user_id: uuid.UUID, *, now: datetime | None = None
    ) -> list[uuid.UUID]:
        """Organizations the user currently holds any grant in (§26.1, ``GET /orgs``)."""
        grants = self._grants.active_grants_for_user(user_id, at=now)
        seen: dict[uuid.UUID, None] = {}
        for grant in grants:
            seen.setdefault(grant.organization_id, None)
        return list(seen)

    def organization_access(
        self, user_id: uuid.UUID, *, now: datetime | None = None
    ) -> list[OrganizationAccess]:
        """Every organization the caller can act in, with resolved permissions.

        Backs ``GET /auth/me``. Organizations are fetched in one batched query rather than one
        per grant, and the soonest expiry is surfaced so a client can warn that a break-glass
        window is closing.
        """
        grants = self._grants.active_grants_for_user(user_id, at=now)
        by_organization: dict[uuid.UUID, list[UserOrganizationAccess]] = {}
        for grant in grants:
            by_organization.setdefault(grant.organization_id, []).append(grant)

        organizations = self._organizations.get_many(list(by_organization))
        summaries: list[OrganizationAccess] = []
        for organization_id, held in by_organization.items():
            resolved = self.resolve(user_id, organization_id, now=now)
            organization = organizations.get(organization_id)
            expiries = [grant.expires_at for grant in held if grant.expires_at is not None]
            summaries.append(
                OrganizationAccess(
                    organization_id=organization_id,
                    organization_code=organization.organization_code if organization else "",
                    display_name=organization.display_name if organization else "",
                    permissions=resolved.permissions,
                    via_break_glass=resolved.via_break_glass,
                    expires_at=min(expiries) if expiries else None,
                )
            )
        summaries.sort(key=lambda item: item.display_name)
        return summaries

    def _enabled_configurable(
        self, organization_id: uuid.UUID, roles: set[Role]
    ) -> frozenset[Permission]:
        """The organization's enabled ``C`` cells, restricted to cells valid for these roles.

        Intersecting here as well as in :func:`resolve_permissions` is deliberate: it keeps a
        stale override row from even being considered, so the widest a role can ever become is
        the §5.3 matrix.
        """
        if not roles:
            return frozenset()
        by_role = self._overrides.enabled_for_roles(organization_id, [role.value for role in roles])
        allowed: set[Permission] = set()
        for role in roles:
            configurable = configurable_permissions_for(role)
            allowed |= by_role.get(role.value, frozenset()) & configurable
        return frozenset(allowed)

    # ---------------------------------------------------------------- grants

    def grant(
        self,
        actor: ActorContext,
        *,
        user_id: uuid.UUID,
        organization_id: uuid.UUID,
        role: Role,
        role_id: uuid.UUID,
        expires_at: datetime | None = None,
        reason: str | None = None,
        granting_roles: frozenset[Role],
        granting_organization_ids: Sequence[uuid.UUID],
    ) -> UserOrganizationAccess:
        """Grant a role in one organization (§24.4.8 ``POST /users/{uid}/org-access``).

        §5.3 note 4 constrains an Organization Admin: it "can grant only roles OA, HRA, HRE,
        DM, RV, RDR within organizations where the OA holds OA; OA cannot grant SSA and cannot
        grant access to other organizations". Enforced here, because this is the one place a
        grant is created and therefore the only place privilege escalation could originate.
        """
        is_platform_admin = Role.SYSTEM_SUPER_ADMIN in granting_roles
        if not is_platform_admin:
            if role not in ORG_ADMIN_GRANTABLE_ROLES:
                raise PermissionDenied(
                    f"An Organization Admin cannot grant the '{role.value}' role."
                )
            if organization_id not in set(granting_organization_ids):
                raise OrgAccessDenied()

        target = self._users.get(user_id)
        if target is None or target.archived_at is not None:
            raise ResourceNotFound("user")

        grant = UserOrganizationAccess(
            id=uuid.uuid4(),
            user_id=user_id,
            organization_id=organization_id,
            role_id=role_id,
            granted_by=actor.user_id,
            granted_at=utc_now(),
            expires_at=expires_at,
            reason=reason,
            is_break_glass=False,
        )
        self._grants.add(grant)
        self._cache.invalidate(user_id, organization_id)

        self._audit.record(
            AuditEvent(
                action=AuditAction.ORG_ACCESS_GRANTED,
                entity_type="user_organization_access",
                entity_id=grant.id,
                actor_user_id=actor.user_id,
                actor_role_codes=format_actor_roles(granting_roles),
                organization_id=organization_id,
                reason=reason,
                request_id=actor.metadata.request_id,
                ip_address=actor.metadata.ip_address,
                user_agent=actor.metadata.user_agent,
            )
        )
        return grant

    def revoke(
        self,
        actor: ActorContext,
        *,
        grant_id: uuid.UUID,
        reason: str,
        granting_roles: frozenset[Role],
        granting_organization_ids: Sequence[uuid.UUID],
    ) -> UserOrganizationAccess:
        """Revoke a grant (§24.4.8 ``DELETE /users/{uid}/org-access/{grantId}``).

        Effective within 60 seconds by the §5.5 cache ceiling, and immediately on this worker
        because the cache entry is dropped here.
        """
        grant = self._grants.get(grant_id)
        if grant is None:
            raise ResourceNotFound("access grant")
        if Role.SYSTEM_SUPER_ADMIN not in granting_roles and grant.organization_id not in set(
            granting_organization_ids
        ):
            # No existence disclosure: an OA must not learn that a grant exists in an
            # organization they cannot see.
            raise ResourceNotFound("access grant")
        if grant.revoked_at is not None:
            raise ValidationFailed("That access grant is already revoked.")

        self._grants.revoke(grant, by_user_id=actor.user_id, reason=reason)
        self._cache.invalidate(grant.user_id, grant.organization_id)

        self._audit.record(
            AuditEvent(
                action=AuditAction.ORG_ACCESS_REVOKED,
                entity_type="user_organization_access",
                entity_id=grant.id,
                actor_user_id=actor.user_id,
                actor_role_codes=format_actor_roles(granting_roles),
                organization_id=grant.organization_id,
                reason=reason,
                request_id=actor.metadata.request_id,
                ip_address=actor.metadata.ip_address,
                user_agent=actor.metadata.user_agent,
            )
        )
        return grant

    # ---------------------------------------------------------------- break-glass

    def grant_break_glass(
        self,
        actor: ActorContext,
        *,
        organization_id: uuid.UUID,
        reason: str,
        duration_minutes: int,
        hr_admin_role_id: uuid.UUID,
        actor_roles: frozenset[Role],
    ) -> UserOrganizationAccess:
        """Mint a time-bound emergency grant (§5.3 note 1, §24.4.8 ``POST /break-glass``).

        Three limits, all from the spec: the caller must be a System Super Admin, the reason
        must be at least twenty characters, and the duration must not exceed four hours. The
        grant is an ordinary expiring HRA grant (DEC-007), so every downstream permission check
        and audit entry works unchanged — only its provenance differs, which
        ``is_break_glass`` records.
        """
        if Role.SYSTEM_SUPER_ADMIN not in actor_roles:
            raise PermissionDenied("Only a System Super Admin may request break-glass access.")

        cleaned = reason.strip()
        if len(cleaned) < BREAK_GLASS_MIN_REASON_LENGTH:
            raise ValidationFailed(
                f"A break-glass reason must be at least {BREAK_GLASS_MIN_REASON_LENGTH} characters."
            ).with_detail(field="reason", code="TOO_SHORT", message="Explain why access is needed.")
        if not 1 <= duration_minutes <= BREAK_GLASS_MAX_MINUTES:
            raise ValidationFailed(
                f"Break-glass duration must be between 1 and {BREAK_GLASS_MAX_MINUTES} minutes."
            ).with_detail(
                field="duration_minutes",
                code="OUT_OF_RANGE",
                message=f"Maximum {BREAK_GLASS_MAX_MINUTES} minutes.",
            )

        now = utc_now()
        grant = UserOrganizationAccess(
            id=uuid.uuid4(),
            user_id=actor.user_id,
            organization_id=organization_id,
            role_id=hr_admin_role_id,
            granted_by=actor.user_id,
            granted_at=now,
            expires_at=now + timedelta(minutes=duration_minutes),
            reason=cleaned,
            is_break_glass=True,
        )
        self._grants.add(grant)
        self._cache.invalidate(actor.user_id, organization_id)

        # §5.3 note 1: audited into the *target organization's* log, so the organization can
        # see that a platform administrator entered.
        self._audit.record(
            AuditEvent(
                action=AuditAction.BREAK_GLASS_GRANTED,
                entity_type="user_organization_access",
                entity_id=grant.id,
                actor_user_id=actor.user_id,
                actor_role_codes=format_actor_roles(actor_roles),
                organization_id=organization_id,
                reason=cleaned,
                request_id=actor.metadata.request_id,
                ip_address=actor.metadata.ip_address,
                user_agent=actor.metadata.user_agent,
            )
        )
        return grant

    @staticmethod
    def break_glass_role() -> Role:
        return BREAK_GLASS_ROLE

    # ---------------------------------------------------------------- deactivation

    def deactivate_user(
        self, actor: ActorContext, *, user: User, reason: str, actor_roles: frozenset[Role]
    ) -> int:
        """Deactivate a user and cut off their access immediately (§25.5).

        Three things happen together, in one transaction: the account is marked inactive, every
        refresh token is revoked, and every grant is revoked. §25.5 requires refresh-token
        revocation to be *immediate* rather than waiting for expiry; access tokens cannot be
        revoked individually, which is why §NFR-13 caps their lifetime at fifteen minutes.
        """
        user.status = "INACTIVE"
        revoked_tokens = self._refresh_tokens.revoke_all_for_user(
            user.id, reason="USER_DEACTIVATED"
        )
        self._grants.revoke_all_for_user(user.id, by_user_id=actor.user_id)
        self._cache.invalidate_user(user.id)

        self._audit.record(
            AuditEvent(
                action=AuditAction.USER_DEACTIVATED,
                entity_type="user",
                entity_id=user.id,
                actor_user_id=actor.user_id,
                actor_role_codes=format_actor_roles(actor_roles),
                organization_id=None,
                reason=reason,
                request_id=actor.metadata.request_id,
                ip_address=actor.metadata.ip_address,
                user_agent=actor.metadata.user_agent,
            )
        )
        return revoked_tokens


__all__ = [
    "AccessService",
    "OrganizationAccess",
    "PermissionCache",
    "ResolvedAccess",
]
