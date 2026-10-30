"""§24.4.5 documents: category, upload, complete, download, verify, archive, expiry."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.permissions import Role
from tests.factories import create_user
from tests.integration.test_employees import _employee_body, _setup

PDF = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n"


def _category(client: Any, organization_id: Any, **overrides: Any) -> dict:
    body = {
        "category_code": "ID_PROOF",
        "category_name": "Identity Proof",
        "mime_types": ["application/pdf"],
        "requires_verification": True,
        "supports_expiry": True,
    }
    body.update(overrides)
    response = client.post(f"/api/v1/orgs/{organization_id}/document-categories", json=body)
    assert response.status_code == 201, response.text
    return response.json()


def _initiate(
    client: Any, organization_id: Any, employee_id: Any, category_id: Any, **overrides: Any
) -> Any:
    body = {
        "category_id": category_id,
        "document_title": "PAN card",
        "original_file_name": "pan.pdf",
        "declared_mime_type": "application/pdf",
        "file_size_bytes": len(PDF),
        "document_number": "ABCDE1234F",
    }
    body.update(overrides)
    return client.post(
        f"/api/v1/orgs/{organization_id}/employees/{employee_id}/documents",
        json=body,
    )


class TestDocuments:
    def test_upload_complete_download_and_verify(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization, client, masters = _setup(db_session, authenticate)
        employee = client.post(
            f"/api/v1/orgs/{organization.id}/employees", json=_employee_body(masters)
        ).json()
        category = _category(client, organization.id)

        initiated = _initiate(client, organization.id, employee["id"], category["id"])
        assert initiated.status_code == 201, initiated.text
        body = initiated.json()
        assert body["upload_status"] == "PENDING_UPLOAD"
        assert body["masked_document_number"].endswith("234F")
        assert "ABCDE1234F" not in initiated.text

        uploaded = client.put(body["upload_url"], content=PDF)
        assert uploaded.status_code == 204, uploaded.text

        completed = client.post(
            f"/api/v1/orgs/{organization.id}/employees/{employee['id']}/documents/{body['id']}/complete"
        )
        assert completed.status_code == 200, completed.text
        assert completed.json()["upload_status"] == "AVAILABLE"
        assert completed.json()["is_current"] is True

        url = client.post(
            f"/api/v1/orgs/{organization.id}/employees/{employee['id']}/documents/{body['id']}/download-url"
        )
        assert url.status_code == 200, url.text
        downloaded = client.client.get(url.json()["url"])
        assert downloaded.status_code == 200
        assert downloaded.content == PDF

        reviewer = create_user(db_session, grants=[(organization.id, Role.HR_ADMIN)])
        db_session.commit()
        reviewer_client = authenticate(reviewer)
        verified = reviewer_client.post(
            f"/api/v1/orgs/{organization.id}/employees/{employee['id']}/documents/{body['id']}/verify",
            json={"decision": "VERIFIED"},
        )
        assert verified.status_code == 200, verified.text
        assert verified.json()["verification_status"] == "VERIFIED"

    def test_wrong_content_type_is_rejected(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization, client, masters = _setup(db_session, authenticate)
        employee = client.post(
            f"/api/v1/orgs/{organization.id}/employees", json=_employee_body(masters)
        ).json()
        category = _category(client, organization.id)
        body = _initiate(client, organization.id, employee["id"], category["id"]).json()
        assert client.put(body["upload_url"], content=b"X" * len(PDF)).status_code == 204
        completed = client.post(
            f"/api/v1/orgs/{organization.id}/employees/{employee['id']}/documents/{body['id']}/complete"
        )
        assert completed.status_code == 415

    def test_oversize_is_rejected_at_initiation(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization, client, masters = _setup(db_session, authenticate)
        employee = client.post(
            f"/api/v1/orgs/{organization.id}/employees", json=_employee_body(masters)
        ).json()
        category = _category(client, organization.id, max_file_size_bytes=1024 * 1024)
        response = _initiate(
            client,
            organization.id,
            employee["id"],
            category["id"],
            file_size_bytes=2 * 1024 * 1024,
        )
        assert response.status_code == 413

    def test_replacement_supersedes_and_keeps_history(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization, client, masters = _setup(db_session, authenticate)
        employee = client.post(
            f"/api/v1/orgs/{organization.id}/employees", json=_employee_body(masters)
        ).json()
        category = _category(client, organization.id)
        first = _initiate(client, organization.id, employee["id"], category["id"]).json()
        client.put(first["upload_url"], content=PDF)
        client.post(
            f"/api/v1/orgs/{organization.id}/employees/{employee['id']}/documents/{first['id']}/complete"
        )

        replacement = client.post(
            f"/api/v1/orgs/{organization.id}/employees/{employee['id']}/documents/{first['id']}/versions",
            json={
                "category_id": category["id"],
                "document_title": "PAN card (new)",
                "original_file_name": "pan-new.pdf",
                "declared_mime_type": "application/pdf",
                "file_size_bytes": len(PDF),
            },
        )
        assert replacement.status_code == 201, replacement.text
        assert replacement.json()["version_number"] == 2
        client.put(replacement.json()["upload_url"], content=PDF)
        client.post(
            f"/api/v1/orgs/{organization.id}/employees/{employee['id']}/documents/{replacement.json()['id']}/complete"
        )

        listed = client.get(
            f"/api/v1/orgs/{organization.id}/employees/{employee['id']}/documents",
            params={"include_versions": "true"},
        ).json()
        assert len(listed) == 2
        current = [row for row in listed if row["is_current"]]
        assert len(current) == 1
        assert current[0]["version_number"] == 2
        assert current[0]["supersedes_document_id"] == first["id"]

    def test_expiring_list(self, api_client: Any, db_session: Any, authenticate: Any) -> None:
        organization, client, masters = _setup(db_session, authenticate)
        employee = client.post(
            f"/api/v1/orgs/{organization.id}/employees", json=_employee_body(masters)
        ).json()
        category = _category(client, organization.id)
        expiry = datetime.now(UTC).date() + timedelta(days=5)
        body = _initiate(
            client,
            organization.id,
            employee["id"],
            category["id"],
            issue_date="2020-01-01",
            expiry_date=expiry.isoformat(),
        ).json()
        client.put(body["upload_url"], content=PDF)
        client.post(
            f"/api/v1/orgs/{organization.id}/employees/{employee['id']}/documents/{body['id']}/complete"
        )

        response = client.get(
            f"/api/v1/orgs/{organization.id}/documents/expiring", params={"within_days": 30}
        )
        assert response.status_code == 200, response.text
        assert [row["id"] for row in response.json()] == [body["id"]]
        assert response.json()[0]["expiry_state"] == "EXPIRING_SOON"
