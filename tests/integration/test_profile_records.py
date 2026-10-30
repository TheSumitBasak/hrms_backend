"""§24.4.4 remaining profile records: education, previous employment, family, nominations."""

from __future__ import annotations

from typing import Any

from tests.integration.test_employees import _employee_body, _setup


def _employee(client: Any, organization_id: Any, masters: dict[str, Any]) -> dict:
    response = client.post(
        f"/api/v1/orgs/{organization_id}/employees", json=_employee_body(masters)
    )
    assert response.status_code == 201, response.text
    return response.json()


class TestEducation:
    def test_create_edit_and_archive(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization, client, masters = _setup(db_session, authenticate)
        employee = _employee(client, organization.id, masters)
        base = f"/api/v1/orgs/{organization.id}/employees/{employee['id']}/education"

        created = client.post(
            base,
            json={
                "qualification_level": "GRADUATE",
                "degree_name": "B.Tech",
                "institution_name": "Example Institute",
                "passing_year": 2012,
                "result_type": "CGPA",
                "result_value": "8.4",
                "maximum_result_value": 10,
            },
        )
        assert created.status_code == 201, created.text

        current = client.get(base)
        updated = client.patch(
            f"{base}/{created.json()['id']}",
            json={"specialization": "Computer Science"},
            headers={"If-Match": current.headers["etag"]} if "etag" in current.headers else {},
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["specialization"] == "Computer Science"

        archived = client.post(
            f"{base}/{created.json()['id']}/archive", json={"reason": "Duplicate entry"}
        )
        assert archived.status_code == 204

    def test_cgpa_requires_scale(self, api_client: Any, db_session: Any, authenticate: Any) -> None:
        organization, client, masters = _setup(db_session, authenticate)
        employee = _employee(client, organization.id, masters)
        response = client.post(
            f"/api/v1/orgs/{organization.id}/employees/{employee['id']}/education",
            json={
                "qualification_level": "GRADUATE",
                "degree_name": "B.Tech",
                "institution_name": "Example Institute",
                "passing_year": 2012,
                "result_type": "CGPA",
                "result_value": "8.4",
            },
        )
        assert response.status_code == 422


class TestPreviousEmployment:
    def test_overlap_is_flagged_then_accepted(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization, client, masters = _setup(db_session, authenticate)
        employee = _employee(client, organization.id, masters)
        base = f"/api/v1/orgs/{organization.id}/employees/{employee['id']}/previous-employment"

        first = client.post(
            base,
            json={
                "employer_name": "Acme",
                "employment_start_date": "2018-01-01",
                "employment_end_date": "2020-12-31",
            },
        )
        assert first.status_code == 201, first.text
        assert first.json()["overlap_review_status"] == "NOT_APPLICABLE"

        second = client.post(
            base,
            json={
                "employer_name": "Globex",
                "employment_start_date": "2020-01-01",
                "employment_end_date": "2021-12-31",
            },
        )
        assert second.status_code == 201, second.text
        assert second.json()["overlap_review_status"] == "PENDING_REVIEW"

        reviewed = client.post(
            f"{base}/{second.json()['id']}/overlap-review",
            json={"decision": "ACCEPTED", "remarks": "Overlap verified with employee"},
        )
        assert reviewed.status_code == 200, reviewed.text
        assert reviewed.json()["overlap_review_status"] == "ACCEPTED"

    def test_end_before_start_is_rejected(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization, client, masters = _setup(db_session, authenticate)
        employee = _employee(client, organization.id, masters)
        response = client.post(
            f"/api/v1/orgs/{organization.id}/employees/{employee['id']}/previous-employment",
            json={
                "employer_name": "Acme",
                "employment_start_date": "2020-01-01",
                "employment_end_date": "2019-01-01",
            },
        )
        assert response.status_code == 422


class TestFamily:
    def test_create_and_archive(self, api_client: Any, db_session: Any, authenticate: Any) -> None:
        organization, client, masters = _setup(db_session, authenticate)
        employee = _employee(client, organization.id, masters)
        base = f"/api/v1/orgs/{organization.id}/employees/{employee['id']}/family-members"
        created = client.post(
            base,
            json={"full_name": "Rao Senior", "relationship_code": "FATHER"},
        )
        assert created.status_code == 201, created.text
        archived = client.post(
            f"{base}/{created.json()['id']}/archive", json={"reason": "Recorded in error"}
        )
        assert archived.status_code == 204


class TestNominations:
    def test_shares_must_total_one_hundred(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization, client, masters = _setup(db_session, authenticate)
        employee = _employee(client, organization.id, masters)
        base = f"/api/v1/orgs/{organization.id}/employees/{employee['id']}/nominations/PF"

        refused = client.put(
            base,
            json={
                "effective_from": "2026-10-01",
                "nominees": [
                    {
                        "nominee_name": "Asha",
                        "relationship_code": "SPOUSE",
                        "date_of_birth": "1992-01-01",
                        "share_percentage": 60,
                    },
                    {
                        "nominee_name": "Rao",
                        "relationship_code": "SON",
                        "date_of_birth": "2015-01-01",
                        "guardian_name": "Asha",
                        "guardian_relationship": "MOTHER",
                        "share_percentage": 30,
                    },
                ],
            },
        )
        assert refused.status_code == 422

        accepted = client.put(
            base,
            json={
                "effective_from": "2026-10-01",
                "nominees": [
                    {
                        "nominee_name": "Asha",
                        "relationship_code": "SPOUSE",
                        "date_of_birth": "1992-01-01",
                        "share_percentage": 60,
                    },
                    {
                        "nominee_name": "Rao",
                        "relationship_code": "SON",
                        "date_of_birth": "2015-01-01",
                        "guardian_name": "Asha",
                        "guardian_relationship": "MOTHER",
                        "share_percentage": 40,
                    },
                ],
            },
        )
        assert accepted.status_code == 200, accepted.text
        assert float(accepted.json()["total_share"]) == 100.0

        listed = client.get(
            f"/api/v1/orgs/{organization.id}/employees/{employee['id']}/nominations",
            params={"type": "PF"},
        )
        assert listed.status_code == 200
        assert len(listed.json()) == 1

    def test_minor_requires_guardian(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization, client, masters = _setup(db_session, authenticate)
        employee = _employee(client, organization.id, masters)
        response = client.put(
            f"/api/v1/orgs/{organization.id}/employees/{employee['id']}/nominations/PF",
            json={
                "effective_from": "2026-10-01",
                "nominees": [
                    {
                        "nominee_name": "Rao",
                        "relationship_code": "SON",
                        "date_of_birth": "2015-01-01",
                        "share_percentage": 100,
                    },
                ],
            },
        )
        assert response.status_code == 422
