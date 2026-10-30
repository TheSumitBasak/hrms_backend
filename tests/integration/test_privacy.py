"""§25 privacy: notices, export, legal hold, erasure and retention execution."""

from __future__ import annotations

from datetime import date
from typing import Any

from app.core.permissions import Role
from tests.factories import create_user
from tests.integration.test_employees import _employee_body, _setup


def _employee(client: Any, organization_id: Any, masters: dict[str, Any]) -> dict:
    response = client.post(
        f"/api/v1/orgs/{organization_id}/employees", json=_employee_body(masters)
    )
    assert response.status_code == 201, response.text
    return response.json()


class TestPrivacy:
    def test_notice_acknowledge_and_withdraw(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization, client, masters = _setup(db_session, authenticate)
        employee = _employee(client, organization.id, masters)
        base = f"/api/v1/orgs/{organization.id}/employees/{employee['id']}/privacy-notices"

        created = client.post(
            base,
            json={
                "notice_version": "2026.1",
                "basis": "CONSENT",
                "consent_scope": "Optional photograph",
                "delivered_at": "2026-10-01T09:00:00Z",
                "method": "ELECTRONIC",
            },
        )
        assert created.status_code == 201, created.text

        acknowledged = client.post(f"{base}/{created.json()['id']}/acknowledge")
        assert acknowledged.status_code == 200, acknowledged.text
        assert acknowledged.json()["acknowledged_at"]

        withdrawn = client.post(
            f"{base}/{created.json()['id']}/withdraw",
            json={"reason": "Consent no longer given for photograph"},
        )
        assert withdrawn.status_code == 200, withdrawn.text
        assert withdrawn.json()["withdrawn_at"]

    def test_export_is_encrypted_and_needs_the_one_time_password(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization, client, masters = _setup(db_session, authenticate)
        employee = _employee(client, organization.id, masters)
        response = client.post(
            f"/api/v1/orgs/{organization.id}/employees/{employee['id']}/data-export",
            json={"include_documents": False},
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["masked"] is True

        missing = client.client.get(body["download_url"])
        assert missing.status_code == 422
        downloaded = client.client.get(
            body["download_url"], headers={"X-Export-OTP": body["one_time_password"]}
        )
        assert downloaded.status_code == 200, downloaded.text
        import json

        payload = json.loads(downloaded.content)
        assert payload["employee"]["id"] == employee["id"]

    def test_legal_hold_blocks_erasure_until_cleared(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization, client, masters = _setup(db_session, authenticate)
        employee = _employee(client, organization.id, masters)
        base = f"/api/v1/orgs/{organization.id}/employees/{employee['id']}"

        held = client.post(
            f"{base}/legal-hold",
            json={"legal_hold": True, "reason": "Litigation hold from legal team"},
        )
        assert held.status_code == 200, held.text

        request = client.post(f"{base}/erasure-requests", json={})
        assert request.status_code == 201, request.text
        refused = client.post(
            f"{base}/erasure-requests/{request.json()['id']}/decision",
            json={"decision": "APPROVED"},
        )
        assert refused.status_code == 422

        client.post(
            f"{base}/legal-hold",
            json={"legal_hold": False, "reason": "Hold released by legal team"},
        )
        completed = client.post(
            f"{base}/erasure-requests/{request.json()['id']}/decision",
            json={"decision": "APPROVED"},
        )
        assert completed.status_code == 200, completed.text
        assert completed.json()["status"] == "COMPLETED"

        profile = client.get(base)
        assert profile.json()["first_name"] == "ANON"
        assert profile.json()["personal_mobile"] is None

    def test_retention_batch_requires_two_people_and_executes(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization, client, masters = _setup(db_session, authenticate)
        employee = _employee(client, organization.id, masters)
        from app.models.employee import Employee

        db_session.query(Employee).filter(Employee.id == employee["id"]).update(
            {
                "status": "RETIRED",
                "joining_date": date(2014, 1, 1),
                "employment_start_date": date(2014, 1, 1),
                "employment_end_date": date(2015, 1, 1),
            }
        )
        db_session.commit()
        second = create_user(db_session, grants=[(organization.id, Role.HR_ADMIN)])
        db_session.commit()

        proposed = client.post(
            f"/api/v1/orgs/{organization.id}/retention-batches",
            json={
                "scope_description": "Employees separated beyond retention",
                "affected_entity_ids": [employee["id"]],
            },
        )
        assert proposed.status_code == 201, proposed.text

        first_signature = client.post(
            f"/api/v1/orgs/{organization.id}/retention-batches/{proposed.json()['id']}/approve"
        )
        assert first_signature.status_code == 200, first_signature.text
        assert first_signature.json()["status"] == "PROPOSED"

        reviewer = authenticate(second)
        approved = reviewer.post(
            f"/api/v1/orgs/{organization.id}/retention-batches/{proposed.json()['id']}/approve"
        )
        assert approved.status_code == 200, approved.text
        assert approved.json()["status"] == "APPROVED"

        executed = client.post(
            f"/api/v1/orgs/{organization.id}/retention-batches/{proposed.json()['id']}/execute"
        )
        assert executed.status_code == 200, executed.text
        assert executed.json()["status"] == "EXECUTED"
