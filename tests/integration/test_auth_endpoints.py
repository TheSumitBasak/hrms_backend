"""Authentication endpoints end to end against a real database (§24.4.8, §25.5, NFR-13)."""

from __future__ import annotations

from typing import Any

from app.core.errors import ErrorCode
from app.core.permissions import Role
from tests.factories import DEFAULT_PASSWORD, create_organization, create_user

LOGIN = "/api/v1/auth/login"
REFRESH = "/api/v1/auth/refresh"
LOGOUT = "/api/v1/auth/logout"
ME = "/api/v1/auth/me"


class TestLogin:
    def test_valid_credentials_return_a_token_pair(self, api_client: Any, db_session: Any) -> None:
        user = create_user(db_session)
        db_session.commit()

        response = api_client.post(LOGIN, json={"email": user.email, "password": DEFAULT_PASSWORD})
        assert response.status_code == 200
        body = response.json()
        assert body["token_type"] == "Bearer"
        assert body["access_token"] and body["refresh_token"]
        # NFR-13 caps the access token at fifteen minutes.
        assert 0 < body["expires_in"] <= 900

    def test_email_is_matched_case_insensitively(self, api_client: Any, db_session: Any) -> None:
        """§21.1 stores emails lower-cased; a mixed-case sign-in must still work."""
        create_user(db_session, email="Rajesh.Patel@Example.Test")
        db_session.commit()
        response = api_client.post(
            LOGIN, json={"email": "RAJESH.PATEL@EXAMPLE.TEST", "password": DEFAULT_PASSWORD}
        )
        assert response.status_code == 200

    def test_wrong_password_is_rejected(self, api_client: Any, db_session: Any) -> None:
        user = create_user(db_session)
        db_session.commit()
        response = api_client.post(LOGIN, json={"email": user.email, "password": "wrong-password"})
        assert response.status_code == 401
        assert response.json()["error"]["code"] == ErrorCode.UNAUTHENTICATED.value

    def test_unknown_email_and_wrong_password_are_indistinguishable(
        self, api_client: Any, db_session: Any
    ) -> None:
        """OWASP A07: the response must not tell an attacker which addresses exist."""
        user = create_user(db_session)
        db_session.commit()

        wrong_password = api_client.post(
            LOGIN, json={"email": user.email, "password": "wrong-password"}
        )
        unknown_user = api_client.post(
            LOGIN, json={"email": "nobody@example.test", "password": "wrong-password"}
        )
        assert wrong_password.status_code == unknown_user.status_code == 401
        assert wrong_password.json()["error"]["message"] == unknown_user.json()["error"]["message"]

    def test_inactive_account_cannot_sign_in(self, api_client: Any, db_session: Any) -> None:
        user = create_user(db_session, status="INACTIVE")
        db_session.commit()
        response = api_client.post(LOGIN, json={"email": user.email, "password": DEFAULT_PASSWORD})
        assert response.status_code == 401

    def test_oidc_account_cannot_sign_in_with_a_password(
        self, api_client: Any, db_session: Any
    ) -> None:
        """An account meant for the identity provider must not accept a local password."""
        user = create_user(db_session, auth_provider="OIDC", password=None)
        db_session.commit()
        response = api_client.post(LOGIN, json={"email": user.email, "password": DEFAULT_PASSWORD})
        assert response.status_code == 401

    def test_response_never_contains_the_password_hash(
        self, api_client: Any, db_session: Any
    ) -> None:
        user = create_user(db_session)
        db_session.commit()
        response = api_client.post(LOGIN, json={"email": user.email, "password": DEFAULT_PASSWORD})
        assert "$2b$" not in response.text
        assert "password" not in response.text.lower()

    def test_tokens_are_not_cacheable(self, api_client: Any, db_session: Any) -> None:
        """§11.6/§15.6 apply no-store to the whole API; a token response especially."""
        user = create_user(db_session)
        db_session.commit()
        response = api_client.post(LOGIN, json={"email": user.email, "password": DEFAULT_PASSWORD})
        assert "no-store" in response.headers["Cache-Control"]


class TestRefreshRotation:
    def test_refresh_returns_a_new_pair(self, api_client: Any, db_session: Any) -> None:
        user = create_user(db_session)
        db_session.commit()
        first = api_client.post(
            LOGIN, json={"email": user.email, "password": DEFAULT_PASSWORD}
        ).json()

        response = api_client.post(REFRESH, json={"refresh_token": first["refresh_token"]})
        assert response.status_code == 200
        second = response.json()
        assert second["refresh_token"] != first["refresh_token"]

    def test_used_refresh_token_is_consumed(self, api_client: Any, db_session: Any) -> None:
        """Rotation means the presented token stops working."""
        user = create_user(db_session)
        db_session.commit()
        first = api_client.post(
            LOGIN, json={"email": user.email, "password": DEFAULT_PASSWORD}
        ).json()
        api_client.post(REFRESH, json={"refresh_token": first["refresh_token"]})

        replay = api_client.post(REFRESH, json={"refresh_token": first["refresh_token"]})
        assert replay.status_code == 401

    def test_replaying_a_rotated_token_revokes_the_whole_family(
        self, api_client: Any, db_session: Any
    ) -> None:
        """A replay is indistinguishable from theft, so every session is cut.

        The successor token — which a legitimate client still holds — must stop working too.
        """
        user = create_user(db_session)
        db_session.commit()
        first = api_client.post(
            LOGIN, json={"email": user.email, "password": DEFAULT_PASSWORD}
        ).json()
        second = api_client.post(REFRESH, json={"refresh_token": first["refresh_token"]}).json()

        # Replay the already-rotated token.
        api_client.post(REFRESH, json={"refresh_token": first["refresh_token"]})

        after_revocation = api_client.post(REFRESH, json={"refresh_token": second["refresh_token"]})
        assert after_revocation.status_code == 401

    def test_garbage_refresh_token_is_rejected(self, api_client: Any) -> None:
        response = api_client.post(REFRESH, json={"refresh_token": "x" * 40})
        assert response.status_code == 401

    def test_access_token_cannot_be_used_as_a_refresh_token(
        self, api_client: Any, db_session: Any
    ) -> None:
        user = create_user(db_session)
        db_session.commit()
        tokens = api_client.post(
            LOGIN, json={"email": user.email, "password": DEFAULT_PASSWORD}
        ).json()
        response = api_client.post(REFRESH, json={"refresh_token": tokens["access_token"]})
        assert response.status_code == 401


class TestLogout:
    def test_logout_revokes_the_refresh_token(self, api_client: Any, db_session: Any) -> None:
        user = create_user(db_session)
        db_session.commit()
        tokens = api_client.post(
            LOGIN, json={"email": user.email, "password": DEFAULT_PASSWORD}
        ).json()

        assert (
            api_client.post(LOGOUT, json={"refresh_token": tokens["refresh_token"]}).status_code
            == 204
        )
        assert (
            api_client.post(REFRESH, json={"refresh_token": tokens["refresh_token"]}).status_code
            == 401
        )

    def test_logout_is_idempotent(self, api_client: Any, db_session: Any) -> None:
        """A client that has already discarded its tokens is logged out either way."""
        user = create_user(db_session)
        db_session.commit()
        tokens = api_client.post(
            LOGIN, json={"email": user.email, "password": DEFAULT_PASSWORD}
        ).json()
        for _ in range(2):
            assert (
                api_client.post(LOGOUT, json={"refresh_token": tokens["refresh_token"]}).status_code
                == 204
            )

    def test_logout_of_an_unknown_token_still_succeeds(self, api_client: Any) -> None:
        """Reporting "that token was already invalid" would be an oracle."""
        assert api_client.post(LOGOUT, json={"refresh_token": "y" * 40}).status_code == 204


class TestCurrentUser:
    def test_requires_authentication(self, api_client: Any) -> None:
        response = api_client.get(ME)
        assert response.status_code == 401
        assert response.headers["WWW-Authenticate"] == "Bearer"

    def test_rejects_a_malformed_bearer_token(self, api_client: Any) -> None:
        response = api_client.get(ME, headers={"Authorization": "Bearer not-a-jwt"})
        assert response.status_code == 401

    def test_describes_the_caller(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization = create_organization(db_session, seed=False)
        user = create_user(db_session, grants=[(organization.id, Role.HR_ADMIN)])
        db_session.commit()

        response = authenticate(user).get(ME)
        assert response.status_code == 200
        body = response.json()
        assert body["email"] == user.email
        assert len(body["organizations"]) == 1
        entry = body["organizations"][0]
        assert entry["organization_id"] == str(organization.id)
        assert entry["organization_code"] == organization.code
        assert entry["roles"] == ["HRA"]
        assert "identity.reveal" in entry["permissions"]
        assert entry["via_break_glass"] is False

    def test_lists_only_organizations_the_caller_can_access(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        """The §6.1 isolation property, visible at the edge."""
        mine = create_organization(db_session, seed=False)
        theirs = create_organization(db_session, seed=False)
        user = create_user(db_session, grants=[(mine.id, Role.READ_ONLY_VIEWER)])
        db_session.commit()

        body = authenticate(user).get(ME).json()
        organization_ids = {entry["organization_id"] for entry in body["organizations"]}
        assert organization_ids == {str(mine.id)}
        assert str(theirs.id) not in response_text(body)

    def test_reports_no_organizations_when_ungranted(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        user = create_user(db_session)
        db_session.commit()
        assert authenticate(user).get(ME).json()["organizations"] == []

    def test_union_of_roles_is_reported(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        """§5.1: effective permissions are the union of roles granted in that organization."""
        organization = create_organization(db_session, seed=False)
        user = create_user(
            db_session,
            grants=[(organization.id, Role.HR_EXECUTIVE), (organization.id, Role.READ_ONLY_VIEWER)],
        )
        db_session.commit()

        entry = authenticate(user).get(ME).json()["organizations"][0]
        assert sorted(entry["roles"]) == ["HRE", "RV"]

    def test_deactivated_user_token_stops_working(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        """§25.5: the account is re-read per request, so deactivation takes effect at once."""
        from app.models.platform import User

        organization = create_organization(db_session, seed=False)
        user = create_user(db_session, grants=[(organization.id, Role.HR_ADMIN)])
        db_session.commit()
        client = authenticate(user)
        assert client.get(ME).status_code == 200

        db_session.query(User).filter(User.id == user.id).update({"status": "INACTIVE"})
        db_session.commit()

        assert client.get(ME).status_code == 401


def response_text(body: object) -> str:
    """Render a response body for substring assertions about absent data."""
    import json

    return json.dumps(body)
