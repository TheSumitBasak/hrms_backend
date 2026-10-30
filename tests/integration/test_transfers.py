"""§10.9 rehire linkage and inter-organization transfer."""

from __future__ import annotations

from typing import Any

from app.core.permissions import Role
from tests.factories import create_organization, create_user
from tests.integration.test_employees import _employee_body


def _masters(client: Any, organization_id: Any, code: str) -> dict[str, Any]:
    base = f"/api/v1/orgs/{organization_id}"
    branch = client.post(
        base + "/branches",
        json={
            "branch_code": f"BR{code}",
            "branch_name": f"Branch {code}",
            "address_line_1": "Plot 1",
            "city": "Gandhinagar",
            "state": "Gujarat",
            "postal_code": "382025",
        },
    )
    assert branch.status_code == 201, branch.text
    department = client.post(
        base + "/masters/departments", json={"code": f"IT{code}", "name": f"IT {code}"}
    )
    assert department.status_code == 201, department.text
    designation = client.post(
        base + "/masters/designations", json={"code": f"ENG{code}", "name": f"Engineer {code}"}
    )
    assert designation.status_code == 201, designation.text
    employment = next(
        row
        for row in client.get(base + "/masters/employment-types").json()
        if row["base_type"] == "PERMANENT"
    )
    rule = client.post(
        base + "/employee-id-rules",
        json={
            "rule_name": f"Rule {code}",
            "rule_code": f"RULE{code}",
            "format_pattern": "{ORG}{SEP}{SEQ}",
            "separator": "-",
            "effective_from": "2020-01-01",
        },
    )
    assert rule.status_code == 201, rule.text
    client.post(base + f"/employee-id-rules/{rule.json()['id']}/publish")
    return {
        "branch_id": branch.json()["id"],
        "department_id": department.json()["id"],
        "designation_id": designation.json()["id"],
        "employment_type_id": employment["id"],
    }


def test_inter_organization_transfer(api_client: Any, db_session: Any, authenticate: Any) -> None:
    source = create_organization(db_session)
    target = create_organization(db_session)
    user = create_user(
        db_session,
        grants=[
            (source.id, Role.ORGANIZATION_ADMIN),
            (source.id, Role.HR_ADMIN),
            (target.id, Role.ORGANIZATION_ADMIN),
            (target.id, Role.HR_ADMIN),
        ],
    )
    db_session.commit()
    client = authenticate(user)
    source_masters = _masters(client, source.id, "S")
    target_masters = _masters(client, target.id, "T")

    employee = client.post(
        f"/api/v1/orgs/{source.id}/employees", json=_employee_body(source_masters)
    )
    assert employee.status_code == 201, employee.text
    source_id = employee.json()["id"]
    identifier = client.put(
        f"/api/v1/orgs/{source.id}/employees/{source_id}/identifiers/PAN",
        json={"value": "ABCPE1234F"},
    )
    assert identifier.status_code == 200, identifier.text

    transferred = client.post(
        f"/api/v1/orgs/{target.id}/employees/{source_id}/transfers",
        json={
            "target_joining_date": "2026-11-01",
            "target_department_id": target_masters["department_id"],
            "target_designation_id": target_masters["designation_id"],
            "target_employment_type_id": target_masters["employment_type_id"],
            "target_branch_id": target_masters["branch_id"],
            "copy_categories": ["IDENTIFIERS"],
        },
    )
    assert transferred.status_code == 201, transferred.text
    body = transferred.json()
    assert body["employment_transition_type"] == "INTER_ORG_TRANSFER"
    assert body["previous_employment_record_id"] == source_id
    assert body["employee_code"].startswith(target.code)

    source_record = client.get(f"/api/v1/orgs/{source.id}/employees/{source_id}")
    assert source_record.json()["status"] == "ON_NOTICE"
    assert source_record.json()["employment_end_date"] == "2026-10-31"

    copied = client.get(f"/api/v1/orgs/{target.id}/employees/{body['id']}/identifiers")
    assert copied.status_code == 200
    assert copied.json()[0]["identifier_type"] == "PAN"
