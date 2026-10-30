"""O3 correction and O4 employment-end boundary handling."""

from __future__ import annotations

from typing import Any

from tests.integration.test_employees import _setup
from tests.integration.test_salary import TODAY, _component, _employee, _published_template


def test_o3_correction_replaces_without_rewriting_history(
    api_client: Any, db_session: Any, authenticate: Any
) -> None:
    organization, client, masters = _setup(db_session, authenticate)
    component = _component(client, organization.id)
    version = _published_template(client, organization.id, component["id"])
    employee = _employee(client, organization.id, masters)
    base = f"/api/v1/orgs/{organization.id}/employees/{employee['id']}/salary-assignments"
    active = client.post(
        base,
        json={
            "template_id": version["template_id"],
            "effective_from": TODAY,
            "revision_reason": "NEW_JOINER",
            "confirm": True,
        },
    ).json()

    corrected = client.post(
        f"{base}/{active['id']}/correct",
        json={
            "reason": "Component value entered incorrectly at joiner setup",
            "components": [
                {
                    "component_id": component["id"],
                    "value_amount": "55000.00",
                    "override_reason": "Corrected joiner value",
                }
            ],
        },
    )
    assert corrected.status_code == 200, corrected.text
    replacement = corrected.json()
    assert replacement["id"] != active["id"]
    assert replacement["components"][0]["value_amount"] == "55000.00"

    timeline = client.get(base, params={"include_cancelled": "true"}).json()
    original = next(row for row in timeline if row["id"] == active["id"])
    assert original["assignment_status"] == "CANCELLED"
    assert original["superseded_by_assignment_id"] == replacement["id"]


def test_o4_employment_end_adjusts_and_restores_boundary(
    api_client: Any, db_session: Any, authenticate: Any
) -> None:
    organization, client, masters = _setup(db_session, authenticate)
    component = _component(client, organization.id)
    version = _published_template(client, organization.id, component["id"])
    employee = _employee(client, organization.id, masters)
    base = f"/api/v1/orgs/{organization.id}/employees/{employee['id']}"
    assignment = client.post(
        f"{base}/salary-assignments",
        json={
            "template_id": version["template_id"],
            "effective_from": TODAY,
            "revision_reason": "NEW_JOINER",
            "confirm": True,
        },
    ).json()
    if employee["status"] == "DRAFT":
        client.post(f"{base}/activate")

    notice = client.post(
        f"{base}/status-transitions",
        json={
            "to_status": "ON_NOTICE",
            "status_effective_date": TODAY,
            "employment_end_date": "2026-12-31",
            "status_reason": "RESIGNATION",
        },
    )
    assert notice.status_code == 200, notice.text
    timeline = client.get(f"{base}/salary-assignments").json()
    adjusted = next(row for row in timeline if row["id"] == assignment["id"])
    assert adjusted["effective_to"] == "2027-01-01"

    withdrawn = client.post(
        f"{base}/status-transitions",
        json={"to_status": "ACTIVE", "status_effective_date": TODAY},
    )
    assert withdrawn.status_code == 200, withdrawn.text
    timeline = client.get(f"{base}/salary-assignments").json()
    restored = next(row for row in timeline if row["id"] == assignment["id"])
    assert restored["effective_to"] is None
