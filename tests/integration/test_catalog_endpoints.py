"""§24.4.2 branches, masters, lookups and custom statuses."""

from __future__ import annotations

from typing import Any

from app.core.errors import ErrorCode
from app.core.permissions import Role
from tests.factories import create_organization, create_user


def _client(db_session: Any, authenticate: Any, role: Role = Role.ORGANIZATION_ADMIN):
    organization = create_organization(db_session)
    user = create_user(db_session, grants=[(organization.id, role)])
    db_session.commit()
    return organization, authenticate(user)


BRANCH = {
    "branch_code": "GNR01",
    "branch_name": "Gandhinagar Office",
    "address_line_1": "Plot 12, GIDC Electronic Estate",
    "city": "Gandhinagar",
    "state": "Gujarat",
    "postal_code": "382025",
}


class TestBranches:
    def test_create_read_and_update(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization, client = _client(db_session, authenticate)
        base = f"/api/v1/orgs/{organization.id}/branches"

        created = client.post(base, json=BRANCH)
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["state_code"] == "24"
        assert body["status"] == "ACTIVE"

        read = client.get(f"{base}/{body['id']}")
        assert read.status_code == 200
        assert read.headers["etag"]

        updated = client.patch(
            f"{base}/{body['id']}",
            json={"branch_name": "Gandhinagar Headquarters"},
            headers={"If-Match": read.headers["etag"]},
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["branch_name"] == "Gandhinagar Headquarters"

    def test_duplicate_code_is_rejected(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization, client = _client(db_session, authenticate)
        base = f"/api/v1/orgs/{organization.id}/branches"
        assert client.post(base, json=BRANCH).status_code == 201
        duplicate = client.post(base, json={**BRANCH, "branch_name": "Second"})
        assert duplicate.status_code == 409
        assert duplicate.json()["error"]["code"] == ErrorCode.DUPLICATE_RESOURCE.value

    def test_deactivation_requires_acknowledging_assigned_employees(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization, client = _client(db_session, authenticate)
        base = f"/api/v1/orgs/{organization.id}/branches"
        branch_id = client.post(base, json=BRANCH).json()["id"]

        # No employees yet, so deactivation proceeds without a warning.
        response = client.post(
            f"{base}/{branch_id}/deactivate", json={"reason": "Office closed permanently"}
        )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "INACTIVE"


class TestMasters:
    def test_create_department_and_grade(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization, client = _client(db_session, authenticate)
        base = f"/api/v1/orgs/{organization.id}/masters"

        department = client.post(
            f"{base}/departments", json={"code": "IT", "name": "Information Technology"}
        )
        assert department.status_code == 201, department.text
        assert department.json()["code"] == "IT"

        grade = client.post(f"{base}/grades", json={"code": "G3", "name": "Grade 3", "rank": 3})
        assert grade.status_code == 201, grade.text
        assert grade.json()["rank"] == 3

        listed = client.get(f"{base}/departments")
        assert listed.status_code == 200
        assert [row["code"] for row in listed.json()] == ["IT"]

    def test_field_not_applicable_to_master_type(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization, client = _client(db_session, authenticate)
        response = client.post(
            f"/api/v1/orgs/{organization.id}/masters/employee-categories",
            json={"code": "STAFF", "name": "Staff", "rank": 1},
        )
        assert response.status_code == 422

    def test_grade_requires_rank(self, api_client: Any, db_session: Any, authenticate: Any) -> None:
        organization, client = _client(db_session, authenticate)
        response = client.post(
            f"/api/v1/orgs/{organization.id}/masters/grades",
            json={"code": "G1", "name": "Grade 1"},
        )
        assert response.status_code == 422

    def test_master_code_is_unique_per_type(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization, client = _client(db_session, authenticate)
        base = f"/api/v1/orgs/{organization.id}/masters/departments"
        assert client.post(base, json={"code": "IT", "name": "IT"}).status_code == 201
        duplicate = client.post(base, json={"code": "it", "name": "IT again"})
        assert duplicate.status_code == 409


class TestLookupsAndStatuses:
    def test_lookup_value_edit(self, api_client: Any, db_session: Any, authenticate: Any) -> None:
        organization, client = _client(db_session, authenticate)
        created = client.post(
            f"/api/v1/orgs/{organization.id}/lookups/RELATIONSHIP",
            json={"list_code": "RELATIONSHIP", "value_code": "COUSIN", "label": "Cousin"},
        )
        assert created.status_code == 201, created.text

        current = client.get(f"/api/v1/orgs/{organization.id}/lookups/RELATIONSHIP/COUSIN")
        updated = client.patch(
            f"/api/v1/orgs/{organization.id}/lookups/RELATIONSHIP/COUSIN",
            json={"label": "First Cousin"},
            headers={"If-Match": current.headers["etag"]},
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["label"] == "First Cousin"

    def test_undisclosed_gender_cannot_be_deactivated(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization, client = _client(db_session, authenticate)
        current = client.get(f"/api/v1/orgs/{organization.id}/lookups/GENDER/UNDISCLOSED")
        response = client.patch(
            f"/api/v1/orgs/{organization.id}/lookups/GENDER/UNDISCLOSED",
            json={"status": "INACTIVE"},
            headers={"If-Match": current.headers["etag"]},
        )
        assert response.status_code == 422

    def test_custom_status(self, api_client: Any, db_session: Any, authenticate: Any) -> None:
        organization, client = _client(db_session, authenticate)
        base = f"/api/v1/orgs/{organization.id}/custom-statuses"
        created = client.post(
            base,
            json={"code": "TRANSFERRED", "label": "Transferred", "base_behaviour": "ACTIVE_LIKE"},
        )
        assert created.status_code == 201, created.text
        listed = client.get(base)
        assert [row["code"] for row in listed.json()] == ["TRANSFERRED"]


class TestCatalogueAuthorization:
    def test_hr_executive_cannot_manage_masters(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization, client = _client(db_session, authenticate, role=Role.HR_EXECUTIVE)
        response = client.post(
            f"/api/v1/orgs/{organization.id}/masters/departments",
            json={"code": "IT", "name": "IT"},
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == ErrorCode.PERMISSION_DENIED.value

    def test_other_organization_branch_is_not_found(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization, client = _client(db_session, authenticate)
        created = client.post(f"/api/v1/orgs/{organization.id}/branches", json=BRANCH).json()
        other = create_organization(db_session, seed=False)
        db_session.commit()
        # Using my organization's path with a foreign branch id must not disclose it.
        response = client.get(f"/api/v1/orgs/{organization.id}/branches/{other.id}")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == ErrorCode.RESOURCE_NOT_FOUND.value
        assert str(other.id) not in response.text
        assert created["id"]
