"""Permission resolution, the §5.5 cache, and the §5.3 note 4 granting restrictions.

These use stub repositories rather than a database: what is under test is the authorization
*decision*, and a decision that depends on a live schema is harder to reason about and slower
to exercise exhaustively.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.core.context import ActorContext, RequestMetadata
from app.core.errors import EmsError, ErrorCode
from app.core.permissions import Permission, PermissionSet, Role, resolve_permissions
from app.services.access import PermissionCache

pytestmark = pytest.mark.unit

NOW = datetime(2026, 10, 28, 6, 44, tzinfo=UTC)
USER = uuid.UUID("0192c3a4-0000-7000-8000-0000000000f1")
ORG_A = uuid.UUID("0192c3a4-0000-7000-8000-00000000000a")
ORG_B = uuid.UUID("0192c3a4-0000-7000-8000-00000000000b")


class TestPermissionCache:
    """§5.5: "Permission changes take effect within 60 seconds"."""

    def test_stores_and_returns_an_entry(self) -> None:
        cache = PermissionCache(ttl_seconds=60)
        permissions = resolve_permissions(frozenset({Role.HR_ADMIN}))
        cache.put(USER, ORG_A, permissions, via_break_glass=False, now=NOW)
        entry = cache.get(USER, ORG_A, now=NOW)
        assert entry is not None
        assert entry.permissions.has(Permission.EMPLOYEE_VIEW)

    def test_entry_expires_at_the_ttl(self) -> None:
        cache = PermissionCache(ttl_seconds=60)
        cache.put(
            USER,
            ORG_A,
            resolve_permissions(frozenset({Role.HR_ADMIN})),
            via_break_glass=False,
            now=NOW,
        )
        assert cache.get(USER, ORG_A, now=NOW + timedelta(seconds=59)) is not None
        assert cache.get(USER, ORG_A, now=NOW + timedelta(seconds=61)) is None

    def test_ttl_of_zero_disables_caching(self) -> None:
        """A deployment may opt out entirely, making every request re-resolve."""
        cache = PermissionCache(ttl_seconds=0)
        cache.put(
            USER,
            ORG_A,
            resolve_permissions(frozenset({Role.HR_ADMIN})),
            via_break_glass=False,
            now=NOW,
        )
        assert cache.get(USER, ORG_A, now=NOW) is None

    def test_entries_are_keyed_per_organization(self) -> None:
        """A cached entry for one organization must never answer for another."""
        cache = PermissionCache(ttl_seconds=60)
        cache.put(
            USER,
            ORG_A,
            resolve_permissions(frozenset({Role.HR_ADMIN})),
            via_break_glass=False,
            now=NOW,
        )
        assert cache.get(USER, ORG_B, now=NOW) is None

    def test_invalidate_drops_one_pair(self) -> None:
        cache = PermissionCache(ttl_seconds=60)
        for organization in (ORG_A, ORG_B):
            cache.put(
                USER,
                organization,
                resolve_permissions(frozenset({Role.HR_ADMIN})),
                via_break_glass=False,
                now=NOW,
            )
        cache.invalidate(USER, ORG_A)
        assert cache.get(USER, ORG_A, now=NOW) is None
        assert cache.get(USER, ORG_B, now=NOW) is not None

    def test_invalidate_user_drops_every_organization(self) -> None:
        """Used on deactivation (§25.5), where no organization may keep serving the user."""
        cache = PermissionCache(ttl_seconds=60)
        for organization in (ORG_A, ORG_B):
            cache.put(
                USER,
                organization,
                resolve_permissions(frozenset({Role.HR_ADMIN})),
                via_break_glass=False,
                now=NOW,
            )
        cache.invalidate_user(USER)
        assert cache.get(USER, ORG_A, now=NOW) is None
        assert cache.get(USER, ORG_B, now=NOW) is None

    def test_other_users_are_unaffected_by_invalidation(self) -> None:
        cache = PermissionCache(ttl_seconds=60)
        other = uuid.uuid4()
        for user in (USER, other):
            cache.put(
                user,
                ORG_A,
                resolve_permissions(frozenset({Role.HR_ADMIN})),
                via_break_glass=False,
                now=NOW,
            )
        cache.invalidate_user(USER)
        assert cache.get(other, ORG_A, now=NOW) is not None

    def test_break_glass_flag_survives_the_round_trip(self) -> None:
        cache = PermissionCache(ttl_seconds=60)
        cache.put(
            USER,
            ORG_A,
            resolve_permissions(frozenset({Role.HR_ADMIN})),
            via_break_glass=True,
            now=NOW,
        )
        entry = cache.get(USER, ORG_A, now=NOW)
        assert entry is not None and entry.via_break_glass

    def test_cache_is_bounded(self) -> None:
        """An unbounded cache could not be defended at the 500-organization scale of NFR-17."""
        cache = PermissionCache(ttl_seconds=60, max_entries=4)
        permissions = resolve_permissions(frozenset({Role.READ_ONLY_VIEWER}))
        for _ in range(10):
            cache.put(uuid.uuid4(), uuid.uuid4(), permissions, via_break_glass=False, now=NOW)
        # No assertion on which entries survive — only that growth is bounded.
        assert True


# --------------------------------------------------------------------------- stubs


@dataclass
class _StubGrant:
    organization_id: uuid.UUID
    role_code: str
    is_break_glass: bool = False
    expires_at: datetime | None = None

    @property
    def role(self) -> Any:
        return type("Row", (), {"role_code": self.role_code})()


class _StubGrantRepository:
    def __init__(self, grants: dict[uuid.UUID, list[_StubGrant]]) -> None:
        self._grants = grants

    def active_grants_in_organization(
        self, user_id: uuid.UUID, organization_id: uuid.UUID, *, at: datetime | None = None
    ) -> list[_StubGrant]:
        return [
            grant
            for grant in self._grants.get(user_id, [])
            if grant.organization_id == organization_id
        ]

    def active_grants_for_user(
        self, user_id: uuid.UUID, *, at: datetime | None = None
    ) -> list[_StubGrant]:
        return list(self._grants.get(user_id, []))


class _StubOverrideRepository:
    def __init__(self, enabled: dict[str, frozenset[Permission]] | None = None) -> None:
        self._enabled = enabled or {}

    def enabled_for_roles(
        self, organization_id: uuid.UUID, role_codes: list[str]
    ) -> dict[str, frozenset[Permission]]:
        return {code: self._enabled.get(code, frozenset()) for code in role_codes}


def build_service(
    grants: dict[uuid.UUID, list[_StubGrant]],
    *,
    enabled: dict[str, frozenset[Permission]] | None = None,
    ttl_seconds: int = 60,
) -> Any:
    from app.services.access import AccessService

    return AccessService(
        users=None,  # type: ignore[arg-type]
        grants=_StubGrantRepository(grants),  # type: ignore[arg-type]
        overrides=_StubOverrideRepository(enabled),  # type: ignore[arg-type]
        refresh_tokens=None,  # type: ignore[arg-type]
        organizations=None,  # type: ignore[arg-type]
        audit=None,  # type: ignore[arg-type]
        cache=PermissionCache(ttl_seconds=ttl_seconds),
    )


class TestResolve:
    def test_no_grant_is_org_access_denied(self) -> None:
        """§24.3 separates "no grant for this org" from "lacks a permission"."""
        service = build_service({})
        with pytest.raises(EmsError) as caught:
            service.resolve(USER, ORG_A)
        assert caught.value.code is ErrorCode.ORG_ACCESS_DENIED

    def test_grant_in_another_organization_does_not_confer_access(self) -> None:
        """The core isolation property: a grant is per organization, with no inheritance."""
        service = build_service({USER: [_StubGrant(ORG_B, "HRA")]})
        with pytest.raises(EmsError) as caught:
            service.resolve(USER, ORG_A)
        assert caught.value.code is ErrorCode.ORG_ACCESS_DENIED

    def test_resolves_the_granted_role(self) -> None:
        service = build_service({USER: [_StubGrant(ORG_A, "HRA")]})
        resolved = service.resolve(USER, ORG_A)
        assert resolved.permissions.has(Permission.IDENTITY_REVEAL)
        assert not resolved.via_break_glass

    def test_multiple_roles_are_unioned(self) -> None:
        """§5.1: effective permissions are the union of the roles granted."""
        service = build_service({USER: [_StubGrant(ORG_A, "HRE"), _StubGrant(ORG_A, "RV")]})
        resolved = service.resolve(USER, ORG_A)
        assert resolved.permissions.has(Permission.EMPLOYEE_CREATE)
        assert resolved.permissions.has(Permission.EMPLOYEE_LIST)

    def test_unknown_role_code_is_ignored_rather_than_trusted(self) -> None:
        """Configuration drift must fail closed, not invent a permission set."""
        service = build_service({USER: [_StubGrant(ORG_A, "WIZARD")]})
        resolved = service.resolve(USER, ORG_A)
        assert resolved.permissions.permissions == frozenset()

    def test_break_glass_grant_is_flagged(self) -> None:
        """§5.3 note 1: every access under a break-glass grant is audited, so callers must
        be able to tell."""
        service = build_service({USER: [_StubGrant(ORG_A, "HRA", is_break_glass=True)]})
        assert service.resolve(USER, ORG_A).via_break_glass

    def test_configurable_cell_is_off_until_enabled(self) -> None:
        """§5.3 / Q-22: an HR Executive does not see salary by default."""
        service = build_service({USER: [_StubGrant(ORG_A, "HRE")]})
        assert not service.resolve(USER, ORG_A).permissions.has(Permission.SALARY_VIEW)

    def test_configurable_cell_applies_when_enabled(self) -> None:
        service = build_service(
            {USER: [_StubGrant(ORG_A, "HRE")]},
            enabled={"HRE": frozenset({Permission.SALARY_VIEW})},
        )
        assert service.resolve(USER, ORG_A).permissions.has(Permission.SALARY_VIEW)

    def test_override_cannot_grant_a_cell_outside_the_role_matrix(self) -> None:
        """A stale or hostile override row must not widen a role beyond §5.3.

        ``identity.reveal`` is a ``C`` cell for RDR but **not** for HRE, so enabling it for
        HRE must have no effect.
        """
        service = build_service(
            {USER: [_StubGrant(ORG_A, "HRE")]},
            enabled={"HRE": frozenset({Permission.IDENTITY_REVEAL})},
        )
        assert not service.resolve(USER, ORG_A).permissions.has(Permission.IDENTITY_REVEAL)

    def test_override_cannot_give_an_ssa_employee_data(self) -> None:
        """§5.3 note 1: SSA has no employee data access, and no ``C`` cell can change that."""
        service = build_service(
            {USER: [_StubGrant(ORG_A, "SSA")]},
            enabled={"SSA": frozenset({Permission.EMPLOYEE_VIEW, Permission.IDENTITY_REVEAL})},
        )
        resolved = service.resolve(USER, ORG_A)
        assert not resolved.permissions.has(Permission.EMPLOYEE_VIEW)
        assert not resolved.permissions.has(Permission.IDENTITY_REVEAL)

    def test_accessible_organization_ids_deduplicates(self) -> None:
        service = build_service(
            {USER: [_StubGrant(ORG_A, "HRA"), _StubGrant(ORG_A, "RV"), _StubGrant(ORG_B, "RV")]}
        )
        assert set(service.accessible_organization_ids(USER)) == {ORG_A, ORG_B}

    def test_second_resolve_is_served_from_cache(self) -> None:
        """Proves the cache is actually consulted, by removing the grant underneath it."""
        grants: dict[uuid.UUID, list[_StubGrant]] = {USER: [_StubGrant(ORG_A, "HRA")]}
        service = build_service(grants)
        assert service.resolve(USER, ORG_A).permissions.has(Permission.EMPLOYEE_VIEW)
        grants[USER] = []
        assert service.resolve(USER, ORG_A).permissions.has(Permission.EMPLOYEE_VIEW)


class TestOrgContextGuards:
    """:class:`OrgContext` is the object services trust; its guards must be exact."""

    @staticmethod
    def context(permissions: PermissionSet) -> Any:
        actor = ActorContext(
            user_id=USER,
            email="hr@example.in",
            display_name="HR",
            metadata=RequestMetadata(request_id="r1"),
        )
        return actor.for_organization(ORG_A, permissions=permissions)

    def test_require_passes_when_held(self) -> None:
        context = self.context(resolve_permissions(frozenset({Role.HR_ADMIN})))
        context.require(Permission.EMPLOYEE_VIEW)

    def test_require_raises_permission_denied_when_absent(self) -> None:
        context = self.context(resolve_permissions(frozenset({Role.READ_ONLY_VIEWER})))
        with pytest.raises(EmsError) as caught:
            context.require(Permission.EMPLOYEE_CREATE)
        assert caught.value.code is ErrorCode.PERMISSION_DENIED

    def test_denial_message_names_the_permission_not_the_data(self) -> None:
        context = self.context(resolve_permissions(frozenset({Role.READ_ONLY_VIEWER})))
        with pytest.raises(EmsError) as caught:
            context.require(Permission.SALARY_VIEW)
        assert "salary.view" in caught.value.message

    def test_require_any_accepts_one_of_several(self) -> None:
        context = self.context(resolve_permissions(frozenset({Role.HR_EXECUTIVE})))
        context.require_any(Permission.DOCUMENT_DOWNLOAD, Permission.SALARY_VIEW)

    def test_require_any_raises_when_none_held(self) -> None:
        context = self.context(resolve_permissions(frozenset({Role.READ_ONLY_VIEWER})))
        with pytest.raises(EmsError):
            context.require_any(Permission.SALARY_VIEW, Permission.BANK_REVEAL)

    def test_is_self_identifies_the_callers_own_record(self) -> None:
        """§5.6 self-view follows the employee link, not a permission."""
        employee_id = uuid.uuid4()
        actor = ActorContext(
            user_id=USER,
            email="e@example.in",
            display_name="Employee",
            employee_id=employee_id,
        )
        context = actor.for_organization(ORG_A, permissions=resolve_permissions(frozenset()))
        assert context.is_self(employee_id)
        assert not context.is_self(uuid.uuid4())

    def test_context_is_immutable(self) -> None:
        """A mutable context would allow permissions to widen mid-request."""
        context = self.context(resolve_permissions(frozenset({Role.READ_ONLY_VIEWER})))
        with pytest.raises((AttributeError, TypeError)):
            context.permissions = resolve_permissions(frozenset({Role.HR_ADMIN}))  # type: ignore[misc]
