"""§24.4.4 bank accounts: masking, primary invariant and reveal."""

from __future__ import annotations

from typing import Any

from tests.integration.test_employees import _employee_body, _setup

ACCOUNT = {
    "account_holder_name": "Asha Rao",
    "bank_name": "State Bank of India",
    "account_number": "30123456789",
    "ifsc_code": "SBIN0001234",
    "account_type": "SAVINGS",
    "effective_from": "2026-10-01",
}


def _employee(client: Any, organization_id: Any, masters: dict[str, Any], first: str) -> dict:
    response = client.post(
        f"/api/v1/orgs/{organization_id}/employees",
        json=_employee_body(masters, first_name=first),
    )
    assert response.status_code == 201, response.text
    return response.json()


class TestBankAccounts:
    def test_add_lists_masked_and_reveals(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization, client, masters = _setup(db_session, authenticate)
        employee = _employee(client, organization.id, masters, "Asha")
        base = f"/api/v1/orgs/{organization.id}/employees/{employee['id']}/bank-accounts"

        created = client.post(base, json={**ACCOUNT, "is_primary": True})
        assert created.status_code == 201, created.text
        assert created.json()["masked_account_number"].endswith("6789")
        assert "30123456789" not in created.text
        assert created.json()["is_primary"] is True

        revealed = client.post(
            f"{base}/{created.json()['id']}/reveal",
            json={"reason": "Payroll reference verification"},
        )
        assert revealed.status_code == 200, revealed.text
        assert revealed.json()["account_number"] == "30123456789"

    def test_only_one_primary_at_a_time(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization, client, masters = _setup(db_session, authenticate)
        employee = _employee(client, organization.id, masters, "Asha")
        base = f"/api/v1/orgs/{organization.id}/employees/{employee['id']}/bank-accounts"

        first = client.post(base, json=ACCOUNT).json()
        second = client.post(
            base,
            json={
                **ACCOUNT,
                "account_number": "40123456780",
                "ifsc_code": "HDFC0001234",
            },
        ).json()

        changed = client.post(
            f"{base}/{first['id']}/set-primary", json={"effective_from": "2026-10-01"}
        )
        assert changed.status_code == 200, changed.text
        changed = client.post(
            f"{base}/{second['id']}/set-primary", json={"effective_from": "2026-10-02"}
        )
        assert changed.status_code == 200, changed.text

        listed = client.get(base).json()
        primaries = [row for row in listed if row["is_primary"]]
        assert len(primaries) == 1
        assert primaries[0]["id"] == second["id"]

    def test_duplicate_account_needs_acknowledgement(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization, client, masters = _setup(db_session, authenticate)
        first = _employee(client, organization.id, masters, "Asha")
        second = _employee(client, organization.id, masters, "Bala")
        first_url = f"/api/v1/orgs/{organization.id}/employees/{first['id']}/bank-accounts"
        second_url = f"/api/v1/orgs/{organization.id}/employees/{second['id']}/bank-accounts"
        assert client.post(first_url, json=ACCOUNT).status_code == 201

        unacknowledged = client.post(second_url, json=ACCOUNT)
        assert unacknowledged.status_code == 422
        assert unacknowledged.json()["error"]["code"] == "WARNINGS_REQUIRE_ACKNOWLEDGEMENT"

        acknowledged = client.post(
            second_url,
            json={**ACCOUNT, "acknowledged_warnings": ["W-BANK-DUP"]},
        )
        assert acknowledged.status_code == 201, acknowledged.text
