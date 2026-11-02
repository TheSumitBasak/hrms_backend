"""Payroll addendum §4.5 / §15.2: payroll attributes of statutory registrations."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select

from app.core.permissions import Role
from app.db.types import uuid7
from app.models.branch import Branch
from app.models.org_config import OrganizationRegistration
from app.models.payroll import StatutoryRegistrationSetting
from app.payroll.periods import first_of_month
from tests.factories import create_organization, create_user, unique_suffix

PAN = "ABCDE1234F"


def _login(db_session: Any, authenticate: Any, *roles: Role) -> tuple[Any, Any]:
    organization = create_organization(db_session)
    user = create_user(db_session, grants=[(organization.id, role) for role in roles])
    db_session.commit()
    return organization, authenticate(user)


def _admin(db_session: Any, authenticate: Any, *, enable: bool = True) -> tuple[Any, Any, str]:
    organization, client = _login(
        db_session, authenticate, Role.PAYROLL_ADMINISTRATOR, Role.STATUTORY_COMPLIANCE_OFFICER
    )
    base = f"/api/v1/orgs/{organization.id}"
    if enable:
        from datetime import UTC, datetime

        go_live = first_of_month(datetime.now(UTC).date()).isoformat()
        response = client.put(
            f"{base}/payroll-settings",
            json={"enable_payroll_module": True, "payroll_go_live_period": go_live},
        )
        assert response.status_code == 200, response.text
    return organization, client, base


def _registration(db_session: Any, organization_id: Any, registration_type: str) -> Any:
    row = OrganizationRegistration(
        id=uuid7(),
        organization_id=organization_id,
        registration_type=registration_type,
        registration_number=f"{registration_type[:3]}{unique_suffix()}".upper(),
        state_code="24"
        if registration_type in {"GSTIN", "PT_REGISTRATION", "PT_ENROLMENT", "LWF"}
        else None,
        status="ACTIVE",
    )
    db_session.add(row)
    db_session.commit()
    return row.id


def _branch(db_session: Any, organization_id: Any) -> Any:
    suffix = unique_suffix()
    row = Branch(
        id=uuid7(),
        organization_id=organization_id,
        branch_code=f"B{suffix[:8]}".upper(),
        branch_name=f"Branch {suffix}",
        address_line_1="1 Main Road",
        city="Gandhinagar",
        state="Gujarat",
        state_code="24",
        country="IN",
        postal_code="382010",
        status="ACTIVE",
    )
    db_session.add(row)
    db_session.commit()
    return row.id


def _url(base: str, registration_id: Any) -> str:
    return f"{base}/statutory-registration-settings/{registration_id}"


class TestPfAndEsi:
    def test_create_read_and_replace_branches(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization, client, base = _admin(db_session, authenticate)
        registration = _registration(db_session, organization.id, "PF_ESTABLISHMENT")
        branch = _branch(db_session, organization.id)
        url = _url(base, registration)
        assert client.get(url).status_code == 404

        body = {
            "pf_contribution_rate_category": "STANDARD",
            "branches": [{"branch_id": str(branch), "effective_from": "2026-04-01"}],
        }
        created = client.put(url, json=body)
        assert created.status_code == 200, created.text
        data = created.json()
        assert created.headers["ETag"] == f'"{data["row_version"]}"'
        assert data["pf_exempted_trust"] is False
        assert data["branches"][0]["branch_id"] == str(branch)
        assert client.get(url).json()["branches"] == data["branches"]

        # Re-sending the same branch row must not collide with the row it replaces.
        again = client.put(url, json=body, headers={"If-Match": str(data["row_version"])})
        assert again.status_code == 200, again.text
        assert len(again.json()["branches"]) == 1

        cleared = client.put(
            url,
            json={"pf_contribution_rate_category": "REDUCED", "branches": []},
            headers={"If-Match": str(again.json()["row_version"])},
        )
        assert cleared.status_code == 200, cleared.text
        assert cleared.json()["branches"] == []
        assert cleared.json()["pf_contribution_rate_category"] == "REDUCED"

    def test_replace_requires_the_current_etag(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization, client, base = _admin(db_session, authenticate)
        url = _url(base, _registration(db_session, organization.id, "ESIC_EMPLOYER"))
        first = client.put(url, json={"esi_implemented_area": False}).json()
        stale = client.put(
            url,
            json={"esi_implemented_area": True},
            headers={"If-Match": str(first["row_version"] + 5)},
        )
        assert stale.status_code in {409, 412}, stale.text
        assert client.get(url).json()["esi_implemented_area"] is False

    def test_trust_name_is_required_iff_exempted(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization, client, base = _admin(db_session, authenticate)
        url = _url(base, _registration(db_session, organization.id, "PF_ESTABLISHMENT"))
        assert client.put(url, json={"pf_exempted_trust": True}).status_code == 422
        assert client.put(url, json={"pf_trust_name": "Acme Trust"}).status_code == 422
        ok = client.put(url, json={"pf_exempted_trust": True, "pf_trust_name": "Acme Trust"})
        assert ok.status_code == 200, ok.text

    def test_overlapping_periods_for_one_branch_are_refused(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization, client, base = _admin(db_session, authenticate)
        url = _url(base, _registration(db_session, organization.id, "PF_ESTABLISHMENT"))
        branch = str(_branch(db_session, organization.id))
        overlapping = [
            {"branch_id": branch, "effective_from": "2026-04-01", "effective_to": "2026-10-01"},
            {"branch_id": branch, "effective_from": "2026-09-01"},
        ]
        refused = client.put(url, json={"branches": overlapping})
        assert refused.status_code == 422, refused.text
        adjacent = [overlapping[0], {"branch_id": branch, "effective_from": "2026-10-01"}]
        assert client.put(url, json={"branches": adjacent}).status_code == 200

    def test_a_backwards_interval_is_refused(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization, client, base = _admin(db_session, authenticate)
        url = _url(base, _registration(db_session, organization.id, "PF_ESTABLISHMENT"))
        branch = str(_branch(db_session, organization.id))
        item = {"branch_id": branch, "effective_from": "2026-04-01", "effective_to": "2026-04-01"}
        assert client.put(url, json={"branches": [item]}).status_code == 422

    def test_another_organizations_branch_is_not_found(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization, client, base = _admin(db_session, authenticate)
        url = _url(base, _registration(db_session, organization.id, "PF_ESTABLISHMENT"))
        other = create_organization(db_session)
        foreign = _branch(db_session, other.id)
        item = {"branch_id": str(foreign), "effective_from": "2026-04-01"}
        assert client.put(url, json={"branches": [item]}).status_code == 404


class TestTan:
    def test_pan_is_sealed_and_only_masked_in_responses(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization, client, base = _admin(db_session, authenticate)
        registration = _registration(db_session, organization.id, "TAN")
        url = _url(base, registration)
        body = {
            "deductor_category": "COMPANY",
            "responsible_person_name": "A. Signatory",
            "responsible_person_designation": "Director",
            "responsible_person_pan": PAN,
        }
        response = client.put(url, json=body)
        assert response.status_code == 200, response.text
        assert PAN not in response.text
        assert PAN not in client.get(url).text
        assert response.json()["responsible_person_pan_masked"].endswith("234F")

        stored = db_session.execute(
            select(StatutoryRegistrationSetting).where(
                StatutoryRegistrationSetting.registration_id == registration
            )
        ).scalar_one()
        assert stored.responsible_person_pan_encrypted is not None
        assert PAN.encode() not in stored.responsible_person_pan_encrypted

        cleared = client.put(
            url,
            json={"deductor_category": "COMPANY"},
            headers={"If-Match": str(response.json()["row_version"])},
        )
        assert cleared.json()["responsible_person_pan_masked"] is None

    def test_a_malformed_pan_is_refused(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization, client, base = _admin(db_session, authenticate)
        url = _url(base, _registration(db_session, organization.id, "TAN"))
        assert client.put(url, json={"responsible_person_pan": "abc"}).status_code == 422


class TestApplicability:
    def test_fields_of_another_type_are_refused(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization, client, base = _admin(db_session, authenticate)
        tan = _url(base, _registration(db_session, organization.id, "TAN"))
        pf = _url(base, _registration(db_session, organization.id, "PF_ESTABLISHMENT"))
        assert (
            client.put(tan, json={"pf_contribution_rate_category": "STANDARD"}).status_code == 422
        )
        assert client.put(pf, json={"deductor_category": "COMPANY"}).status_code == 422
        assert client.put(pf, json={"esi_implemented_area": False}).status_code == 422

    def test_branches_apply_only_to_pf_and_esi(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization, client, base = _admin(db_session, authenticate)
        url = _url(base, _registration(db_session, organization.id, "TAN"))
        item = {
            "branch_id": str(_branch(db_session, organization.id)),
            "effective_from": "2026-04-01",
        }
        assert client.put(url, json={"branches": [item]}).status_code == 422

    def test_a_registration_type_without_payroll_attributes_is_refused(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization, client, base = _admin(db_session, authenticate)
        url = _url(base, _registration(db_session, organization.id, "GSTIN"))
        assert client.put(url, json={}).status_code == 422

    def test_an_empty_body_is_valid_for_a_pt_registration(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization, client, base = _admin(db_session, authenticate)
        url = _url(base, _registration(db_session, organization.id, "PT_REGISTRATION"))
        assert client.put(url, json={}).status_code == 200


class TestAccess:
    def test_unknown_and_foreign_registrations_are_not_found(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        _, client, base = _admin(db_session, authenticate)
        other = create_organization(db_session)
        foreign = _registration(db_session, other.id, "TAN")
        db_session.commit()
        for registration in (uuid.uuid4(), foreign):
            assert client.get(_url(base, registration)).status_code == 404
            assert client.put(_url(base, registration), json={}).status_code == 404

    def test_roles_without_statutory_config_are_refused(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization, hr = _login(db_session, authenticate, Role.HR_ADMIN)
        url = _url(
            f"/api/v1/orgs/{organization.id}", _registration(db_session, organization.id, "TAN")
        )
        assert hr.get(url).status_code == 403
        assert hr.put(url, json={}).status_code == 403

    def test_writing_requires_the_payroll_module(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization, client, base = _admin(db_session, authenticate, enable=False)
        url = _url(base, _registration(db_session, organization.id, "TAN"))
        response = client.put(url, json={})
        assert response.status_code == 403, response.text
        assert response.json()["error"]["code"] == "MODULE_NOT_ENABLED"
