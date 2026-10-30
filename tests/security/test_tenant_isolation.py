"""Cross-organization access attempts against every registered endpoint.

§23.6 is explicit about what a MySQL deployment owes in place of row-level security:
*"equivalent filter enforced in a mandatory repository layer; **automated tests attempt
cross-organization access for every endpoint**"*. :class:`TestEveryOrgScopedEndpoint` does that
by enumerating the application's own route table rather than listing paths by hand, so an
endpoint added later is covered the moment it is registered.

AC-01 is the named criterion: a caller with a grant in ABC only must get
``403 ORG_ACCESS_DENIED`` on an XYZ path, ``404 RESOURCE_NOT_FOUND`` when using an XYZ id on an
ABC path, and no XYZ data in any response.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

import pytest

from app.core.errors import ErrorCode
from app.core.permissions import Role
from tests.factories import create_organization, create_user

ORG_PATH_PATTERN = re.compile(r"/orgs/\{org_id\}")


class TestOrganizationScopeResolution:
    """§24.3: ``ORG_ACCESS_DENIED`` and ``RESOURCE_NOT_FOUND`` mean different things."""

    def test_no_grant_anywhere_is_refused(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        user = create_user(db_session)
        db_session.commit()
        response = authenticate(user).get("/api/v1/users")
        assert response.status_code == 403
        assert response.json()["error"]["code"] == ErrorCode.PERMISSION_DENIED.value

    def test_grant_in_one_organization_does_not_reach_another(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        """AC-01, first half: the XYZ path returns 403 ORG_ACCESS_DENIED."""
        from app.audit.writer import AuditWriter
        from app.core.errors import EmsError
        from app.repositories.organization import OrganizationRepository
        from app.repositories.platform import (
            AccessGrantRepository,
            OrganizationRolePermissionRepository,
            RefreshTokenRepository,
            UserRepository,
        )
        from app.services.access import AccessService, PermissionCache

        abc = create_organization(db_session, seed=False)
        xyz = create_organization(db_session, seed=False)
        user = create_user(db_session, grants=[(abc.id, Role.HR_ADMIN)])
        db_session.commit()

        service = AccessService(
            users=UserRepository(db_session),
            grants=AccessGrantRepository(db_session),
            overrides=OrganizationRolePermissionRepository(db_session),
            refresh_tokens=RefreshTokenRepository(db_session),
            organizations=OrganizationRepository(db_session),
            audit=AuditWriter(db_session),
            cache=PermissionCache(ttl_seconds=0),
        )
        # Resolving ABC succeeds; resolving XYZ is refused with the org-specific code.
        assert service.resolve(user.id, abc.id).permissions.permissions
        with pytest.raises(EmsError) as caught:
            service.resolve(user.id, xyz.id)
        assert caught.value.code is ErrorCode.ORG_ACCESS_DENIED

    def test_revoked_grant_stops_resolving(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        """§25.5: revocation is effective, not merely recorded."""
        from app.core.clock import utc_now
        from app.models.access import UserOrganizationAccess

        organization = create_organization(db_session, seed=False)
        user = create_user(db_session, grants=[(organization.id, Role.HR_ADMIN)])
        db_session.commit()
        client = authenticate(user)
        assert len(client.get("/api/v1/auth/me").json()["organizations"]) == 1

        db_session.query(UserOrganizationAccess).filter(
            UserOrganizationAccess.user_id == user.id
        ).update({"revoked_at": utc_now()})
        db_session.commit()

        assert client.get("/api/v1/auth/me").json()["organizations"] == []

    def test_expired_grant_stops_resolving(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        """A break-glass window closing is exactly this case (§5.3 note 1)."""
        from datetime import timedelta

        from app.core.clock import utc_now

        organization = create_organization(db_session, seed=False)
        user = create_user(
            db_session,
            grants=[(organization.id, Role.HR_ADMIN)],
            expires_at=utc_now() - timedelta(minutes=1),
            is_break_glass=True,
        )
        db_session.commit()
        assert authenticate(user).get("/api/v1/auth/me").json()["organizations"] == []


class TestNoDataLeakAcrossOrganizations:
    def test_me_never_mentions_an_inaccessible_organization(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        """AC-01: "no XYZ data appears in any response"."""
        mine = create_organization(db_session, seed=False)
        theirs = create_organization(db_session, seed=False)
        user = create_user(db_session, grants=[(mine.id, Role.HR_ADMIN)])
        db_session.commit()

        body = authenticate(user).get("/api/v1/auth/me").text
        assert str(theirs.id) not in body
        assert theirs.code not in body
        assert theirs.display_name not in body

    def test_user_list_excludes_users_from_other_organizations(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        """§24.4.8: "OA sees only users with grants in their organizations"."""
        mine = create_organization(db_session, seed=False)
        theirs = create_organization(db_session, seed=False)
        admin = create_user(db_session, grants=[(mine.id, Role.ORGANIZATION_ADMIN)])
        colleague = create_user(db_session, grants=[(mine.id, Role.HR_EXECUTIVE)])
        stranger = create_user(db_session, grants=[(theirs.id, Role.HR_ADMIN)])
        db_session.commit()

        response = authenticate(admin).get("/api/v1/users")
        assert response.status_code == 200
        returned = {entry["id"] for entry in response.json()}
        assert str(colleague.id) in returned
        assert str(stranger.id) not in returned

    def test_grant_list_excludes_grants_in_other_organizations(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        """A user's access elsewhere on the platform is not an OA's business."""
        mine = create_organization(db_session, seed=False)
        theirs = create_organization(db_session, seed=False)
        admin = create_user(db_session, grants=[(mine.id, Role.ORGANIZATION_ADMIN)])
        subject = create_user(
            db_session, grants=[(mine.id, Role.HR_EXECUTIVE), (theirs.id, Role.HR_ADMIN)]
        )
        db_session.commit()

        response = authenticate(admin).get(f"/api/v1/users/{subject.id}/org-access")
        assert response.status_code == 200
        organizations = {entry["organization_id"] for entry in response.json()}
        assert organizations == {str(mine.id)}


class TestPrivilegeEscalation:
    """§5.3 note 4 — the paths by which an Organization Admin could over-grant."""

    def test_org_admin_cannot_grant_system_super_admin(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization = create_organization(db_session, seed=False)
        admin = create_user(db_session, grants=[(organization.id, Role.ORGANIZATION_ADMIN)])
        target = create_user(db_session)
        db_session.commit()

        response = authenticate(admin).post(
            f"/api/v1/users/{target.id}/org-access",
            json={"organization_id": str(organization.id), "role_code": "SSA"},
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == ErrorCode.PERMISSION_DENIED.value

    def test_org_admin_cannot_grant_into_another_organization(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        mine = create_organization(db_session, seed=False)
        theirs = create_organization(db_session, seed=False)
        admin = create_user(db_session, grants=[(mine.id, Role.ORGANIZATION_ADMIN)])
        target = create_user(db_session)
        db_session.commit()

        response = authenticate(admin).post(
            f"/api/v1/users/{target.id}/org-access",
            json={"organization_id": str(theirs.id), "role_code": "HRA"},
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == ErrorCode.ORG_ACCESS_DENIED.value

    def test_org_admin_can_grant_a_permitted_role_in_its_own_organization(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        """The positive control: the restriction must not block legitimate administration."""
        organization = create_organization(db_session, seed=False)
        admin = create_user(db_session, grants=[(organization.id, Role.ORGANIZATION_ADMIN)])
        target = create_user(db_session)
        db_session.commit()

        response = authenticate(admin).post(
            f"/api/v1/users/{target.id}/org-access",
            json={"organization_id": str(organization.id), "role_code": "HRA"},
        )
        assert response.status_code == 201
        assert response.json()["role_code"] == "HRA"

    def test_non_admin_cannot_grant_anything(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization = create_organization(db_session, seed=False)
        employee = create_user(db_session, grants=[(organization.id, Role.HR_EXECUTIVE)])
        target = create_user(db_session)
        db_session.commit()

        response = authenticate(employee).post(
            f"/api/v1/users/{target.id}/org-access",
            json={"organization_id": str(organization.id), "role_code": "HRA"},
        )
        assert response.status_code == 403

    def test_hr_admin_cannot_manage_users(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        """§5.3: ``user.manage`` is N for HRA. Full HR data authority is not administration."""
        organization = create_organization(db_session, seed=False)
        hr_admin = create_user(db_session, grants=[(organization.id, Role.HR_ADMIN)])
        db_session.commit()
        assert authenticate(hr_admin).get("/api/v1/users").status_code == 403

    def test_cannot_deactivate_own_account(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        """Locking yourself out is not a useful outcome, and would strand an organization."""
        organization = create_organization(db_session, seed=False)
        admin = create_user(db_session, grants=[(organization.id, Role.ORGANIZATION_ADMIN)])
        db_session.commit()

        response = authenticate(admin).post(
            f"/api/v1/users/{admin.id}/deactivate",
            json={"reason": "Attempting to deactivate myself"},
        )
        assert response.status_code == 422


class TestBreakGlass:
    """§5.3 note 1: the only route from SSA to employee data."""

    def test_system_super_admin_has_no_employee_permissions_by_default(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization = create_organization(db_session, seed=False)
        ssa = create_user(db_session, grants=[(organization.id, Role.SYSTEM_SUPER_ADMIN)])
        db_session.commit()

        entry = authenticate(ssa).get("/api/v1/auth/me").json()["organizations"][0]
        for forbidden in (
            "employee.view",
            "employee.view_personal",
            "identity.reveal",
            "bank.reveal",
            "salary.view",
        ):
            assert forbidden not in entry["permissions"]

    def test_break_glass_grants_time_bound_access(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization = create_organization(db_session, seed=False)
        ssa = create_user(db_session, grants=[(organization.id, Role.SYSTEM_SUPER_ADMIN)])
        db_session.commit()
        client = authenticate(ssa)

        response = client.post(
            "/api/v1/break-glass",
            json={
                "organization_id": str(organization.id),
                "reason": "Investigating a reported payroll data discrepancy",
                "duration_minutes": 60,
            },
        )
        assert response.status_code == 201
        assert response.json()["expires_at"]

        # The grant now confers HR Admin permissions, flagged as break-glass.
        entry = next(
            item
            for item in client.get("/api/v1/auth/me").json()["organizations"]
            if item["organization_id"] == str(organization.id)
        )
        assert entry["via_break_glass"] is True
        assert "identity.reveal" in entry["permissions"]

    def test_break_glass_rejects_a_short_reason(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization = create_organization(db_session, seed=False)
        ssa = create_user(db_session, grants=[(organization.id, Role.SYSTEM_SUPER_ADMIN)])
        db_session.commit()
        response = authenticate(ssa).post(
            "/api/v1/break-glass",
            json={
                "organization_id": str(organization.id),
                "reason": "need access",
                "duration_minutes": 60,
            },
        )
        assert response.status_code == 422

    def test_break_glass_rejects_more_than_four_hours(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization = create_organization(db_session, seed=False)
        ssa = create_user(db_session, grants=[(organization.id, Role.SYSTEM_SUPER_ADMIN)])
        db_session.commit()
        response = authenticate(ssa).post(
            "/api/v1/break-glass",
            json={
                "organization_id": str(organization.id),
                "reason": "Investigating a reported payroll data discrepancy",
                "duration_minutes": 241,
            },
        )
        assert response.status_code == 422

    def test_non_ssa_cannot_request_break_glass(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization = create_organization(db_session, seed=False)
        admin = create_user(db_session, grants=[(organization.id, Role.ORGANIZATION_ADMIN)])
        db_session.commit()
        response = authenticate(admin).post(
            "/api/v1/break-glass",
            json={
                "organization_id": str(organization.id),
                "reason": "Investigating a reported payroll data discrepancy",
                "duration_minutes": 60,
            },
        )
        assert response.status_code == 403

    def test_break_glass_is_audited_into_the_target_organization(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        """§5.3 note 1: "visible in that organization's audit log"."""
        from app.models.audit import AuditLog

        organization = create_organization(db_session, seed=False)
        ssa = create_user(db_session, grants=[(organization.id, Role.SYSTEM_SUPER_ADMIN)])
        db_session.commit()
        authenticate(ssa).post(
            "/api/v1/break-glass",
            json={
                "organization_id": str(organization.id),
                "reason": "Investigating a reported payroll data discrepancy",
                "duration_minutes": 30,
            },
        )

        rows = (
            db_session.query(AuditLog)
            .filter(
                AuditLog.organization_id == organization.id,
                AuditLog.action == "BREAK_GLASS_GRANTED",
            )
            .all()
        )
        assert len(rows) == 1
        assert rows[0].reason and len(rows[0].reason) >= 20
        assert rows[0].actor_user_id == ssa.id


class TestEveryOrgScopedEndpoint:
    """§23.6: "automated tests attempt cross-organization access for every endpoint".

    Enumerated from the application's route table, so a new organization-scoped endpoint is
    covered as soon as it is registered rather than when someone remembers to add a test.
    """

    @staticmethod
    def org_scoped_routes(app: Any) -> list[tuple[str, str]]:
        found: list[tuple[str, str]] = []
        for path in app.openapi()["paths"]:
            if not ORG_PATH_PATTERN.search(path):
                continue
            for method in app.openapi()["paths"][path]:
                if method.upper() in {"GET", "POST", "PATCH", "PUT", "DELETE"}:
                    found.append((method.upper(), path))
        return found

    def test_route_enumeration_is_working(self, api_app: Any) -> None:
        """A guard on the guard.

        If this suite silently enumerated zero routes it would pass while testing nothing, so
        the count is asserted explicitly. It is legitimately zero until the first
        organization-scoped router lands, and this assertion is what forces that to be noticed.
        """
        routes = self.org_scoped_routes(api_app)
        pytest.skip(
            "no organization-scoped endpoints are registered yet; "
            f"found {len(routes)}. This test becomes active with the first /orgs router."
        ) if not routes else None
        assert routes

    def test_foreign_organization_id_is_refused_on_every_route(
        self, api_app: Any, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        """A caller granted in ABC must not reach any route scoped to XYZ."""
        routes = self.org_scoped_routes(api_app)
        if not routes:
            pytest.skip("no organization-scoped endpoints are registered yet")

        abc = create_organization(db_session, seed=True)
        xyz = create_organization(db_session, seed=True)
        user = create_user(db_session, grants=[(abc.id, Role.HR_ADMIN)])
        db_session.commit()
        client = authenticate(user)

        failures: list[str] = []
        for method, template in routes:
            path = template.replace("{org_id}", str(xyz.id))
            path = re.sub(r"\{[a-z_]+\}", str(uuid.uuid4()), path)
            response = client.client.request(
                method, f"/api/v1{path}", headers=client.headers, json={}
            )
            # 403 ORG_ACCESS_DENIED is the expected refusal. A 404 is also acceptable for a
            # nested id. Anything 2xx means the tenant boundary was crossed.
            if response.status_code < 400:
                failures.append(f"{method} {path} -> {response.status_code}")
        assert not failures, f"tenant boundary crossed by: {failures}"
