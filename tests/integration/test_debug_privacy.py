from typing import Any

from fastapi.testclient import TestClient

from tests.integration.test_employees import _employee_body, _setup


def test_debug(api_app: Any, db_session: Any, authenticate: Any) -> None:
    organization, client, masters = _setup(db_session, authenticate)
    employee = client.post(
        f"/api/v1/orgs/{organization.id}/employees", json=_employee_body(masters)
    ).json()
    base = f"/api/v1/orgs/{organization.id}/employees/{employee['id']}"
    r = client.post(f"{base}/erasure-requests", json={})
    print(r.text)
    with TestClient(api_app, raise_server_exceptions=True) as raw:
        response = raw.post(
            f"{base}/erasure-requests/{r.json()['id']}/decision",
            json={"decision": "APPROVED"},
            headers={"Authorization": f"Bearer {client.access_token}"},
        )
        assert response.status_code == 200
