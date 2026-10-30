"""§24.4.4 identifiers: encryption at rest, masking, duplicates and audited reveal."""

from __future__ import annotations

from typing import Any

from app.core.errors import ErrorCode
from tests.integration.test_employees import _employee_body, _setup

PAN = "ABCPE1234F"


def _employee(client: Any, organization_id: Any, masters: dict[str, Any], first: str) -> dict:
    response = client.post(
        f"/api/v1/orgs/{organization_id}/employees",
        json=_employee_body(masters, first_name=first),
    )
    assert response.status_code == 201, response.text
    return response.json()


class TestIdentifiers:
    def test_put_masks_and_reveals_with_audit(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization, client, masters = _setup(db_session, authenticate)
        employee = _employee(client, organization.id, masters, "Asha")
        base = f"/api/v1/orgs/{organization.id}/employees/{employee['id']}/identifiers"

        created = client.put(f"{base}/PAN", json={"value": PAN})
        assert created.status_code == 200, created.text
        assert created.json()["masked_value"].endswith(PAN[-4:])
        assert PAN not in created.text

        listed = client.get(base)
        assert listed.status_code == 200
        assert listed.json()[0]["identifier_type"] == "PAN"

        revealed = client.post(
            f"{base}/PAN/reveal", json={"reason": "Statutory verification request"}
        )
        assert revealed.status_code == 200, revealed.text
        assert revealed.json()["value"] == PAN

        audit = client.get(f"/api/v1/orgs/{organization.id}/audit-logs")
        assert any(row["action"] == "IDENTIFIER_REVEALED" for row in audit.json())

    def test_not_collected_type_is_refused(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization, client, masters = _setup(db_session, authenticate)
        employee = _employee(client, organization.id, masters, "Asha")
        response = client.put(
            f"/api/v1/orgs/{organization.id}/employees/{employee['id']}/identifiers/AADHAAR",
            json={"value": "234567890124"},
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == ErrorCode.FIELD_NOT_COLLECTED.value

    def test_duplicate_is_blocked_unless_overridden(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization, client, masters = _setup(db_session, authenticate)
        first = _employee(client, organization.id, masters, "Asha")
        second = _employee(client, organization.id, masters, "Bala")
        first_url = f"/api/v1/orgs/{organization.id}/employees/{first['id']}/identifiers/PAN"
        second_url = f"/api/v1/orgs/{organization.id}/employees/{second['id']}/identifiers/PAN"
        assert client.put(first_url, json={"value": PAN}).status_code == 200

        duplicate = client.put(second_url, json={"value": PAN})
        assert duplicate.status_code == 409
        assert duplicate.json()["error"]["code"] == ErrorCode.DUPLICATE_IDENTIFIER.value

        overridden = client.put(
            second_url,
            json={
                "value": PAN,
                "duplicate_override_reason": "Same person rehired under a new record",
            },
        )
        assert overridden.status_code == 200, overridden.text

    def test_duplicate_precheck_never_returns_plaintext(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization, client, masters = _setup(db_session, authenticate)
        employee = _employee(client, organization.id, masters, "Asha")
        client.put(
            f"/api/v1/orgs/{organization.id}/employees/{employee['id']}/identifiers/PAN",
            json={"value": PAN},
        )
        response = client.post(
            f"/api/v1/orgs/{organization.id}/identifier-duplicate-check",
            json={"identifier_type": "PAN", "value": PAN},
        )
        assert response.status_code == 200, response.text
        assert PAN not in response.text
        assert response.json()["matches"][0]["employee_id"] == employee["id"]

    def test_employee_search_by_identifier_uses_blind_index(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization, client, masters = _setup(db_session, authenticate)
        employee = _employee(client, organization.id, masters, "Asha")
        client.put(
            f"/api/v1/orgs/{organization.id}/employees/{employee['id']}/identifiers/PAN",
            json={"value": PAN},
        )
        response = client.get(
            f"/api/v1/orgs/{organization.id}/employees",
            params={"identifier_type": "PAN", "identifier_value": PAN},
        )
        assert response.status_code == 200, response.text
        assert [row["id"] for row in response.json()["items"]] == [employee["id"]]
        assert PAN not in response.text

    def test_cross_organization_identifier_access_is_not_found(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        from tests.factories import create_organization

        organization, client, _masters = _setup(db_session, authenticate)
        other = create_organization(db_session, seed=False)
        db_session.commit()
        response = client.get(f"/api/v1/orgs/{organization.id}/employees/{other.id}/identifiers")
        assert response.status_code == 404
