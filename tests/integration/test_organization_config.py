"""§24.4.1 configuration endpoints: registrations, identifier and nomination policies."""

from __future__ import annotations

from typing import Any

from app.core.errors import ErrorCode
from app.core.permissions import Role
from tests.conftest import fictitious_gstin
from tests.factories import create_organization, create_user


def _organization_admin(db_session: Any, **organization_kwargs: Any):
    organization = create_organization(db_session, **organization_kwargs)
    user = create_user(db_session, grants=[(organization.id, Role.ORGANIZATION_ADMIN)])
    db_session.commit()
    return organization, user


class TestRegistrations:
    def test_create_list_and_duplicate(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization, user = _organization_admin(db_session)
        client = authenticate(user)
        base = f"/api/v1/orgs/{organization.id}/registrations"

        created = client.post(
            base,
            json={
                "registration_type": "TAN",
                "registration_number": "MUMA12345B",
                "issuing_authority": "Income Tax Department",
            },
        )
        assert created.status_code == 201, created.text
        assert created.json()["registration_number"] == "MUMA12345B"

        listed = client.get(base)
        assert listed.status_code == 200
        assert len(listed.json()) == 1

        duplicate = client.post(
            base,
            json={"registration_type": "TAN", "registration_number": "MUMA 12345 B"},
        )
        assert duplicate.status_code == 409
        assert duplicate.json()["error"]["code"] == ErrorCode.DUPLICATE_RESOURCE.value

    def test_gstin_pan_mismatch_is_rejected(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization, user = _organization_admin(db_session)
        # The factory does not set a PAN; give the organization one so the GSTIN check has a
        # comparison. A GSTIN carrying a different PAN must be refused (§7.4 E-GSTIN-PAN).
        from app.models.organization import Organization

        db_session.query(Organization).filter(Organization.id == organization.id).update(
            {"pan": "ZZZZZ9999Z"}
        )
        db_session.commit()
        client = authenticate(user)
        response = client.post(
            f"/api/v1/orgs/{organization.id}/registrations",
            json={
                "registration_type": "GSTIN",
                "registration_number": fictitious_gstin(pan="YYYYY8888Y"),
                "state_code": "24",
            },
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == ErrorCode.GSTIN_PAN_MISMATCH.value

    def test_other_requires_label_and_state_scoped_requires_state(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization, user = _organization_admin(db_session)
        client = authenticate(user)
        base = f"/api/v1/orgs/{organization.id}/registrations"

        no_label = client.post(
            base, json={"registration_type": "OTHER", "registration_number": "TL-123"}
        )
        assert no_label.status_code == 422

        no_state = client.post(
            base, json={"registration_type": "LWF", "registration_number": "GJ/LWF/1"}
        )
        assert no_state.status_code == 422

    def test_cross_organization_registrations_are_refused(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        _mine, user = _organization_admin(db_session)
        theirs = create_organization(db_session, seed=False)
        db_session.commit()
        response = authenticate(user).get(f"/api/v1/orgs/{theirs.id}/registrations")
        assert response.status_code == 403
        assert response.json()["error"]["code"] == ErrorCode.ORG_ACCESS_DENIED.value


class TestIdentifierPolicies:
    def test_collecting_aadhaar_requires_a_purpose(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization, user = _organization_admin(db_session)
        client = authenticate(user)
        url = f"/api/v1/orgs/{organization.id}/identifier-policies"

        policies = client.get(url).json()
        assert len(policies) == 9

        payload = [{"identifier_type": "AADHAAR", "requirement": "OPTIONAL"}]
        refused = client.put(url, json=payload)
        assert refused.status_code == 422
        assert refused.json()["error"]["code"] == ErrorCode.VALIDATION_FAILED.value

        accepted = client.put(
            url,
            json=[
                {
                    "identifier_type": "AADHAAR",
                    "requirement": "OPTIONAL",
                    "purpose_statement": "Statutory identity verification for payroll linkage",
                }
            ],
        )
        assert accepted.status_code == 200, accepted.text
        assert any(row["identifier_type"] == "AADHAAR" for row in accepted.json())


class TestNominationConfigs:
    def test_replace_full_allocation_rule(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization, user = _organization_admin(db_session)
        client = authenticate(user)
        url = f"/api/v1/orgs/{organization.id}/nomination-configs"

        listed = client.get(url)
        assert listed.status_code == 200
        assert len(listed.json()) == 4

        response = client.put(
            url,
            json=[
                {
                    "nomination_type": "PF",
                    "requires_full_allocation": True,
                    "requires_dob": True,
                    "max_nominees": 3,
                }
            ],
        )
        assert response.status_code == 200, response.text
        pf = next(row for row in response.json() if row["nomination_type"] == "PF")
        assert pf["max_nominees"] == 3
