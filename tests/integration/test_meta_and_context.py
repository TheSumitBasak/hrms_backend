"""BE-S01 me/context, BE-S03 auth/me additions, BE-S09 /meta."""

from __future__ import annotations

from typing import Any

from app.core.permissions import Role
from tests.factories import create_organization, create_user

META = "/api/v1/meta"
ME = "/api/v1/auth/me"


def context_url(org_id: Any) -> str:
    return f"/api/v1/orgs/{org_id}/me/context"


class TestMeta:
    def test_meta_is_public_and_lists_capabilities(self, api_client: Any) -> None:
        response = api_client.get(META)
        assert response.status_code == 200
        body = response.json()
        assert set(body) == {"api_version", "build_sha", "capabilities"}
        assert "org.me_context" in body["capabilities"]


class TestAuthMe:
    def test_session_policy_and_non_admin_flag(self, authenticate: Any, db_session: Any) -> None:
        org = create_organization(db_session)
        user = create_user(db_session, grants=[(org.id, Role.HR_ADMIN)])
        db_session.commit()

        body = authenticate(user).get(ME).json()
        assert body["is_platform_admin"] is False
        session = body["session"]
        assert session["idle_timeout_seconds"] == 1800
        assert session["absolute_timeout_seconds"] == 12 * 3600
        assert 0 < session["access_token_ttl_seconds"] <= 900

    def test_platform_admin_flag_follows_ssa_grant(
        self, authenticate: Any, db_session: Any
    ) -> None:
        org = create_organization(db_session)
        user = create_user(db_session, grants=[(org.id, Role.SYSTEM_SUPER_ADMIN)])
        db_session.commit()
        assert authenticate(user).get(ME).json()["is_platform_admin"] is True


class TestOrganizationContext:
    def test_any_granted_user_can_read_it(self, authenticate: Any, db_session: Any) -> None:
        org = create_organization(db_session)
        user = create_user(db_session, grants=[(org.id, Role.EMPLOYEE_SELF)])
        db_session.commit()

        response = authenticate(user).get(context_url(org.id))
        assert response.status_code == 200
        body = response.json()
        assert body["organization"]["id"] == str(org.id)
        assert body["organization"]["timezone"] == "Asia/Kolkata"
        assert body["modules"] == {"onboarding": False, "attendance": False, "leave": False}
        assert body["attendance"]["day_close_time"] == "12:00"
        assert body["me"] == {"employee_id": None, "employee_status": None}

    def test_admin_only_settings_are_not_exposed(self, authenticate: Any, db_session: Any) -> None:
        org = create_organization(db_session)
        user = create_user(db_session, grants=[(org.id, Role.EMPLOYEE_SELF)])
        db_session.commit()
        text = authenticate(user).get(context_url(org.id)).text
        for hidden in (
            "org_pan_required",
            "backdated_salary_max_days",
            "allow_manual_employee_code",
        ):
            assert hidden not in text

    def test_other_tenants_context_is_forbidden(self, authenticate: Any, db_session: Any) -> None:
        mine = create_organization(db_session)
        theirs = create_organization(db_session)
        user = create_user(db_session, grants=[(mine.id, Role.HR_ADMIN)])
        db_session.commit()
        assert authenticate(user).get(context_url(theirs.id)).status_code == 403

    def test_requires_authentication(self, api_client: Any, db_session: Any) -> None:
        org = create_organization(db_session)
        db_session.commit()
        assert api_client.get(context_url(org.id)).status_code == 401
