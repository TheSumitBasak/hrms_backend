"""§24.4.3 employee creation, activation, editing and lifecycle."""

from __future__ import annotations

from typing import Any

from app.core.errors import ErrorCode
from app.core.permissions import Role
from tests.factories import create_organization, create_user

BRANCH = {
    "branch_code": "GNR01",
    "branch_name": "Gandhinagar Office",
    "address_line_1": "Plot 12, GIDC Electronic Estate",
    "city": "Gandhinagar",
    "state": "Gujarat",
    "postal_code": "382025",
}

RULE = {
    "rule_name": "Organization-wide codes",
    "rule_code": "ORG-SERIES",
    "format_pattern": "{ORG}{SEP}{SEQ}",
    "separator": "-",
    "sequence_reset_policy": "NEVER",
    "sequence_start": 1,
    "sequence_increment": 1,
    "sequence_padding_length": 4,
    "effective_from": "2020-01-01",
    "priority": 100,
}


def _setup(db_session: Any, authenticate: Any) -> tuple[Any, Any, dict[str, Any]]:
    organization = create_organization(db_session)
    user = create_user(
        db_session,
        grants=[
            (organization.id, Role.ORGANIZATION_ADMIN),
            (organization.id, Role.HR_ADMIN),
        ],
    )
    db_session.commit()
    client = authenticate(user)
    base = f"/api/v1/orgs/{organization.id}"

    branch = client.post(f"{base}/branches", json=BRANCH)
    assert branch.status_code == 201, branch.text
    department = client.post(
        f"{base}/masters/departments", json={"code": "IT", "name": "Information Technology"}
    )
    assert department.status_code == 201, department.text
    designation = client.post(
        f"{base}/masters/designations", json={"code": "ENG", "name": "Engineer"}
    )
    assert designation.status_code == 201, designation.text
    employment_types = client.get(f"{base}/masters/employment-types").json()
    permanent = next(row for row in employment_types if row["base_type"] == "PERMANENT")

    rule = client.post(f"{base}/employee-id-rules", json=RULE)
    assert rule.status_code == 201, rule.text
    published = client.post(f"{base}/employee-id-rules/{rule.json()['id']}/publish")
    assert published.status_code == 200, published.text

    return (
        organization,
        client,
        {
            "branch_id": branch.json()["id"],
            "department_id": department.json()["id"],
            "designation_id": designation.json()["id"],
            "employment_type_id": permanent["id"],
        },
    )


def _employee_body(masters: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    body = {
        "first_name": "Asha",
        "last_name": "Rao",
        "date_of_birth": "1990-05-15",
        "gender": "FEMALE",
        "nationality": "IN",
        "personal_mobile": "+919876543210",
        "joining_date": "2026-10-01",
        "employment_type_id": masters["employment_type_id"],
        "department_id": masters["department_id"],
        "designation_id": masters["designation_id"],
        "branch_id": masters["branch_id"],
        "current_address": {
            "line_1": "22, Shanti Nagar Society",
            "city": "Gandhinagar",
            "state": "Gujarat",
            "postal_code": "382021",
        },
    }
    body.update(overrides)
    return body


class TestEmployeeLifecycle:
    def test_create_draft_with_generated_code(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization, client, masters = _setup(db_session, authenticate)
        response = client.post(
            f"/api/v1/orgs/{organization.id}/employees", json=_employee_body(masters)
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["status"] == "DRAFT"
        assert body["employee_code"] == f"{organization.code}-0001"
        assert body["addresses"]["current"]["city"] == "Gandhinagar"

    def test_activate_requires_mandatory_fields_and_issues_code_on_activation(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization, client, masters = _setup(db_session, authenticate)
        base = f"/api/v1/orgs/{organization.id}"

        # Switch assignment to activation, so the draft has no code yet.
        settings = client.get(f"{base}/settings").json()
        settings.pop("organization_id")
        settings["employee_code_assignment_point"] = "ON_ACTIVATION"
        from app.schemas.common import etag_for

        organization_row = client.get(base).json()
        replaced = client.put(
            f"{base}/settings",
            json=settings,
            headers={"If-Match": etag_for(organization_row["row_version"])},
        )
        assert replaced.status_code == 200, replaced.text

        created = client.post(f"{base}/employees", json=_employee_body(masters))
        assert created.status_code == 201, created.text
        assert created.json()["employee_code"] is None

        activated = client.post(f"{base}/employees/{created.json()['id']}/activate")
        assert activated.status_code == 200, activated.text
        assert activated.json()["status"] == "ACTIVE"
        assert activated.json()["employee_code"] == f"{organization.code}-0001"
        assert activated.json()["status_effective_date"] == "2026-10-01"

    def test_activation_rejects_a_draft_missing_mandatory_data(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization, client, _masters = _setup(db_session, authenticate)
        base = f"/api/v1/orgs/{organization.id}"
        settings = client.get(f"{base}/settings").json()
        settings.pop("organization_id")
        settings["employee_code_assignment_point"] = "ON_ACTIVATION"
        from app.schemas.common import etag_for

        client.put(
            f"{base}/settings",
            json=settings,
            headers={"If-Match": etag_for(client.get(base).json()["row_version"])},
        )
        created = client.post(
            f"{base}/employees",
            json={"first_name": "Incomplete"},
        )
        assert created.status_code == 201, created.text
        response = client.post(
            f"/api/v1/orgs/{organization.id}/employees/{created.json()['id']}/activate"
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == ErrorCode.VALIDATION_FAILED.value

    def test_patch_rejects_job_fields_and_updates_personal_data(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization, client, masters = _setup(db_session, authenticate)
        base = f"/api/v1/orgs/{organization.id}"
        created = client.post(f"{base}/employees", json=_employee_body(masters)).json()

        rejected = client.patch(
            f"{base}/employees/{created['id']}", json={"department_id": masters["department_id"]}
        )
        assert rejected.status_code == 422

        current = client.get(f"{base}/employees/{created['id']}")
        updated = client.patch(
            f"{base}/employees/{created['id']}",
            json={"personal_email": "asha.rao@example.test"},
            headers={"If-Match": current.headers["etag"]},
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["personal_email"] == "asha.rao@example.test"

    def test_status_transition_matrix_and_separated_end_date(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization, client, masters = _setup(db_session, authenticate)
        base = f"/api/v1/orgs/{organization.id}"
        created = client.post(f"{base}/employees", json=_employee_body(masters)).json()
        client.post(f"{base}/employees/{created['id']}/activate")

        notice = client.post(
            f"{base}/employees/{created['id']}/status-transitions",
            json={
                "to_status": "ON_NOTICE",
                "status_effective_date": "2026-10-20",
                "employment_end_date": "2026-11-30",
                "status_reason": "RESIGNATION",
            },
        )
        assert notice.status_code == 200, notice.text
        assert notice.json()["status"] == "ON_NOTICE"

        # A separated status with a future end date is refused; use ON_NOTICE until then.
        early = client.post(
            f"{base}/employees/{created['id']}/status-transitions",
            json={
                "to_status": "RESIGNED",
                "status_effective_date": "2026-10-20",
                "employment_end_date": "2026-11-30",
                "status_reason": "RESIGNATION",
            },
        )
        assert early.status_code == 422

        missing_reason = client.post(
            f"{base}/employees/{created['id']}/status-transitions",
            json={
                "to_status": "INACTIVE",
                "status_effective_date": "2026-10-20",
            },
        )
        assert missing_reason.status_code == 422

        history = client.get(f"{base}/employees/{created['id']}/status-history")
        assert [row["to_status"] for row in history.json()] == ["ON_NOTICE", "ACTIVE"]

    def test_job_change_records_history(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization, client, masters = _setup(db_session, authenticate)
        base = f"/api/v1/orgs/{organization.id}"
        created = client.post(f"{base}/employees", json=_employee_body(masters)).json()
        client.post(f"{base}/employees/{created['id']}/activate")

        grade = client.post(
            f"{base}/masters/grades", json={"code": "G2", "name": "Grade 2", "rank": 2}
        ).json()
        changed = client.post(
            f"{base}/employees/{created['id']}/job-changes",
            json={
                "effective_date": "2026-10-28",
                "change_type": "PROMOTION",
                "grade_id": grade["id"],
                "reason": "Annual promotion cycle",
            },
        )
        assert changed.status_code == 200, changed.text
        assert changed.json()["grade_id"] == grade["id"]

        history = client.get(f"{base}/employees/{created['id']}/job-history")
        assert history.json()[0]["change_type"] == "PROMOTION"

    def test_audit_log_records_employee_changes(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization, client, masters = _setup(db_session, authenticate)
        base = f"/api/v1/orgs/{organization.id}"
        created = client.post(f"{base}/employees", json=_employee_body(masters)).json()

        logs = client.get(f"{base}/audit-logs", params={"entity_type": "employee"})
        assert logs.status_code == 200, logs.text
        assert any(row["action"] == "EMPLOYEE_CREATED" for row in logs.json())

        per_employee = client.get(f"{base}/employees/{created['id']}/audit")
        assert per_employee.status_code == 200
        assert all(row["employee_id"] == created["id"] for row in per_employee.json())

        too_wide = client.get(
            f"{base}/audit-logs",
            params={"from": "2020-01-01T00:00:00", "to": "2026-01-01T00:00:00"},
        )
        assert too_wide.status_code == 422

    def test_age_exception_requires_a_different_decider(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization, client, masters = _setup(db_session, authenticate)
        base = f"/api/v1/orgs/{organization.id}"
        employee = client.post(
            f"{base}/employees",
            json=_employee_body(masters, date_of_birth="2009-05-15"),
        )
        assert employee.status_code == 201, employee.text
        assert employee.json()["age_exception_status"] == "PENDING"
        employee_id = employee.json()["id"]

        self_decision = client.post(
            f"{base}/employees/{employee_id}/age-exception-decision",
            json={"decision": "APPROVED", "reason": "Apprentice programme approval"},
        )
        assert self_decision.status_code == 422

        reviewer = create_user(db_session, grants=[(organization.id, Role.HR_ADMIN)])
        db_session.commit()
        reviewer_client = authenticate(reviewer)
        approved = reviewer_client.post(
            f"{base}/employees/{employee_id}/age-exception-decision",
            json={"decision": "APPROVED", "reason": "Apprentice programme approval"},
        )
        assert approved.status_code == 200, approved.text
        assert approved.json()["age_exception_status"] == "APPROVED"

        activated = reviewer_client.post(f"{base}/employees/{employee_id}/activate")
        assert activated.status_code == 200, activated.text

    def test_cross_organization_employee_is_not_found(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        mine, client, masters = _setup(db_session, authenticate)
        theirs = create_organization(db_session, seed=False)
        db_session.commit()
        created = client.post(
            f"/api/v1/orgs/{mine.id}/employees", json=_employee_body(masters)
        ).json()
        response = client.get(f"/api/v1/orgs/{mine.id}/employees/{theirs.id}")
        assert response.status_code == 404
        assert created["id"]
