"""§26 employee search: filters, sorting and signed cursor pagination."""

from __future__ import annotations

from typing import Any

from tests.integration.test_employees import _employee_body, _setup


def _employee(client: Any, organization_id: Any, masters: dict[str, Any], first: str) -> dict:
    response = client.post(
        f"/api/v1/orgs/{organization_id}/employees",
        json=_employee_body(masters, first_name=first),
    )
    assert response.status_code == 201, response.text
    return response.json()


class TestEmployeeSearch:
    def test_cursor_pagination_is_signed_and_stable(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization, client, masters = _setup(db_session, authenticate)
        names = ["Asha", "Bala", "Chitra"]
        created = [_employee(client, organization.id, masters, name) for name in names]
        url = f"/api/v1/orgs/{organization.id}/employees"

        first = client.get(url, params={"limit": 2, "sort": "display_name:asc"})
        assert first.status_code == 200, first.text
        body = first.json()
        assert len(body["items"]) == 2
        assert body["next_cursor"]

        second = client.get(
            url,
            params={"limit": 2, "sort": "display_name:asc", "cursor": body["next_cursor"]},
        )
        assert second.status_code == 200, second.text
        assert len(second.json()["items"]) == 1
        assert second.json()["next_cursor"] is None

        ids = [row["id"] for row in body["items"] + second.json()["items"]]
        assert sorted(ids) == sorted(row["id"] for row in created)

    def test_tampered_cursor_is_rejected(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization, client, masters = _setup(db_session, authenticate)
        _employee(client, organization.id, masters, "Asha")
        _employee(client, organization.id, masters, "Bala")
        url = f"/api/v1/orgs/{organization.id}/employees"
        body = client.get(url, params={"limit": 1}).json()
        tampered = body["next_cursor"] + "x"
        response = client.get(url, params={"cursor": tampered})
        assert response.status_code == 422

    def test_filter_and_total(self, api_client: Any, db_session: Any, authenticate: Any) -> None:
        organization, client, masters = _setup(db_session, authenticate)
        asha = _employee(client, organization.id, masters, "Asha")
        _employee(client, organization.id, masters, "Bala")
        url = f"/api/v1/orgs/{organization.id}/employees"
        response = client.get(
            url,
            params={
                "employee_code": asha["employee_code"],
                "with_total": "true",
            },
        )
        assert response.status_code == 200, response.text
        assert len(response.json()["items"]) == 1
        assert response.json()["total_count"] == 1

    def test_unknown_sort_is_rejected(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization, client, _masters = _setup(db_session, authenticate)
        response = client.get(
            f"/api/v1/orgs/{organization.id}/employees", params={"sort": "salary"}
        )
        assert response.status_code == 422
