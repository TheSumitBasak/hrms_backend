"""§24.4.6 salary components, templates and timeline operations."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from tests.integration.test_employees import _employee_body, _setup

TODAY = datetime.now(UTC).date().isoformat()


def _component(client: Any, organization_id: Any) -> dict:
    response = client.post(
        f"/api/v1/orgs/{organization_id}/salary-components",
        json={
            "component_code": "BASIC",
            "component_name": "Basic",
            "component_category": "EARNINGS",
            "value_type": "FIXED_AMOUNT",
            "amount_period": "MONTHLY",
            "currency": "INR",
            "is_required": True,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _published_template(client: Any, organization_id: Any, component_id: Any) -> dict:
    created = client.post(
        f"/api/v1/orgs/{organization_id}/salary-templates",
        json={
            "template_code": "STANDARD",
            "template_name": "Standard structure",
            "currency": "INR",
            "effective_from": "2026-01-01",
            "mappings": [
                {
                    "component_id": component_id,
                    "default_value_amount": "50000.00",
                    "is_required": True,
                }
            ],
        },
    )
    assert created.status_code == 201, created.text
    version = created.json()
    published = client.post(
        f"/api/v1/orgs/{organization_id}/salary-templates/{version['template_id']}/versions/{version['id']}/publish"
    )
    assert published.status_code == 200, published.text
    return published.json()


def _employee(client: Any, organization_id: Any, masters: dict[str, Any]) -> dict:
    response = client.post(
        f"/api/v1/orgs/{organization_id}/employees", json=_employee_body(masters)
    )
    assert response.status_code == 201, response.text
    return response.json()


class TestSalary:
    def test_component_template_and_confirm(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization, client, masters = _setup(db_session, authenticate)
        component = _component(client, organization.id)
        version = _published_template(client, organization.id, component["id"])
        employee = _employee(client, organization.id, masters)

        created = client.post(
            f"/api/v1/orgs/{organization.id}/employees/{employee['id']}/salary-assignments",
            json={
                "template_id": version["template_id"],
                "effective_from": TODAY,
                "revision_reason": "NEW_JOINER",
                "confirm": True,
            },
        )
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["assignment_status"] == "ACTIVE"
        assert body["template_code_snapshot"] == "STANDARD"
        assert body["components"][0]["component_code"] == "BASIC"
        assert body["components"][0]["value_amount"] == "50000.00"

        current = client.get(
            f"/api/v1/orgs/{organization.id}/employees/{employee['id']}/salary-assignments/current"
        )
        assert current.status_code == 200, current.text
        assert current.json()["id"] == body["id"]

    def test_future_revision_truncates_predecessor_and_cancel_restores(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization, client, masters = _setup(db_session, authenticate)
        component = _component(client, organization.id)
        version = _published_template(client, organization.id, component["id"])
        employee = _employee(client, organization.id, masters)
        base = f"/api/v1/orgs/{organization.id}/employees/{employee['id']}/salary-assignments"

        first = client.post(
            base,
            json={
                "template_id": version["template_id"],
                "effective_from": TODAY,
                "revision_reason": "NEW_JOINER",
                "confirm": True,
            },
        ).json()
        assert first["effective_to"] is None

        future = client.post(
            base,
            json={
                "template_id": version["template_id"],
                "effective_from": "2027-01-01",
                "revision_reason": "ANNUAL_REVISION",
                "confirm": True,
            },
        )
        assert future.status_code == 201, future.text
        assert future.json()["assignment_status"] == "SCHEDULED"

        timeline = client.get(base).json()
        predecessor = next(row for row in timeline if row["id"] == first["id"])
        assert predecessor["effective_to"] == "2027-01-01"

        cancelled = client.post(
            f"{base}/{future.json()['id']}/cancel",
            json={"reason": "Revision withdrawn before effective date"},
        )
        assert cancelled.status_code == 200, cancelled.text
        assert cancelled.json()["assignment_status"] == "CANCELLED"

        timeline = client.get(base).json()
        restored = next(row for row in timeline if row["id"] == first["id"])
        assert restored["effective_to"] is None

    def test_compare_reports_labels_without_arithmetic(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization, client, masters = _setup(db_session, authenticate)
        component = _component(client, organization.id)
        version = _published_template(client, organization.id, component["id"])
        employee = _employee(client, organization.id, masters)
        base = f"/api/v1/orgs/{organization.id}/employees/{employee['id']}/salary-assignments"

        first = client.post(
            base,
            json={
                "template_id": version["template_id"],
                "effective_from": TODAY,
                "revision_reason": "NEW_JOINER",
                "confirm": True,
            },
        ).json()
        second = client.post(
            base,
            json={
                "template_id": version["template_id"],
                "effective_from": "2027-01-01",
                "revision_reason": "ANNUAL_REVISION",
                "components": [
                    {
                        "component_id": component["id"],
                        "value_amount": "60000.00",
                        "override_reason": "Annual revision award",
                    }
                ],
                "confirm": True,
            },
        ).json()

        compared = client.get(
            f"{base}/compare", params={"left": first["id"], "right": second["id"]}
        )
        assert compared.status_code == 200, compared.text
        row = compared.json()["components"][0]
        assert row["change_type"] == "CHANGED"
        assert row["left_value"] == "50000.00"
        assert row["right_value"] == "60000.00"
        assert "delta" not in compared.text

    def test_custom_components_require_permission_and_setting(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization, client, masters = _setup(db_session, authenticate)
        employee = _employee(client, organization.id, masters)
        response = client.post(
            f"/api/v1/orgs/{organization.id}/employees/{employee['id']}/salary-assignments",
            json={
                "effective_from": TODAY,
                "revision_reason": "NEW_JOINER",
                "components": [
                    {
                        "component_code": "SPECIAL",
                        "component_name": "Special",
                        "component_category": "OTHER",
                        "value_type": "TEXT",
                        "value_text": "Not a number",
                    }
                ],
                "confirm": True,
            },
        )
        assert response.status_code == 422
