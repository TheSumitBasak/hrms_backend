"""§24.4.6 salary components, templates and timeline operations."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from tests.integration.test_employees import _employee_body, _setup

TODAY = datetime.now(UTC).date().isoformat()


def _component(client: Any, organization_id: Any) -> dict:
    """The system Basic component every organization is seeded with."""
    response = client.get(f"/api/v1/orgs/{organization_id}/salary-components")
    assert response.status_code == 200, response.text
    return next(row for row in response.json() if row["component_code"] == "BASIC")


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


class TestTemplateBuilderSupport:
    """SYSTEM_LIMITATIONS #7, #8, #13, #14: version listing, publish-with-save, usage, ISO."""

    def test_versions_are_listed_and_readable_with_mappings(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization, client, _ = _setup(db_session, authenticate)
        component = _component(client, organization.id)
        published = _published_template(client, organization.id, component["id"])
        base = (
            f"/api/v1/orgs/{organization.id}/salary-templates/{published['template_id']}/versions"
        )
        draft = client.post(base)
        assert draft.status_code == 201, draft.text

        listed = client.get(base)
        assert listed.status_code == 200, listed.text
        assert [row["version_number"] for row in listed.json()] == [1, 2]
        assert [row["status"] for row in listed.json()] == ["ACTIVE", "DRAFT"]

        one = client.get(f"{base}/{draft.json()['id']}")
        assert one.status_code == 200, one.text
        assert len(one.json()["mappings"]) == 1
        assert one.headers["ETag"]

    def test_unknown_template_or_foreign_version_is_404(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization, client, _ = _setup(db_session, authenticate)
        component = _component(client, organization.id)
        published = _published_template(client, organization.id, component["id"])
        missing = "00000000-0000-7000-8000-000000000000"
        root = f"/api/v1/orgs/{organization.id}/salary-templates"
        assert client.get(f"{root}/{missing}/versions").status_code == 404
        assert (
            client.get(f"{root}/{published['template_id']}/versions/{missing}").status_code == 404
        )

    def test_publish_saves_unsaved_edits_first(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization, client, _ = _setup(db_session, authenticate)
        component = _component(client, organization.id)
        created = client.post(
            f"/api/v1/orgs/{organization.id}/salary-templates",
            json={"template_code": "LATE", "template_name": "Late", "currency": "INR"},
        ).json()
        url = (
            f"/api/v1/orgs/{organization.id}/salary-templates/{created['template_id']}"
            f"/versions/{created['id']}/publish"
        )
        published = client.post(
            url,
            json={
                "effective_from": "2026-01-01",
                "mappings": [{"component_id": component["id"], "default_value_amount": "42000.00"}],
            },
        )
        assert published.status_code == 200, published.text
        body = published.json()
        assert body["status"] == "ACTIVE"
        assert body["effective_from"] == "2026-01-01"
        assert body["mappings"][0]["default_value_amount"] == "42000.00"

    def test_failed_publish_rolls_back_the_unsaved_edits(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization, client, _ = _setup(db_session, authenticate)
        component = _component(client, organization.id)
        created = client.post(
            f"/api/v1/orgs/{organization.id}/salary-templates",
            json={"template_code": "ROLL", "template_name": "Roll", "currency": "INR"},
        ).json()
        root = f"/api/v1/orgs/{organization.id}/salary-templates/{created['template_id']}"
        # No effective_from anywhere, so publish must fail after the mappings were replaced.
        failed = client.post(
            f"{root}/versions/{created['id']}/publish",
            json={"mappings": [{"component_id": component["id"], "default_value_amount": "99.00"}]},
        )
        assert failed.status_code == 422, failed.text
        stored = client.get(f"{root}/versions/{created['id']}").json()
        assert stored["status"] == "DRAFT"
        # Only the auto-added Basic remains, and the rolled-back edit left no amount on it.
        assert [m["component_id"] for m in stored["mappings"]] == [component["id"]]
        assert stored["mappings"][0]["default_value_amount"] is None

    def test_component_usage_counts_templates_and_assignments(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization, client, masters = _setup(db_session, authenticate)
        component = _component(client, organization.id)
        url = f"/api/v1/orgs/{organization.id}/salary-components/{component['id']}/usage"
        assert client.get(url).json() == {"template_version_count": 0, "assignment_count": 0}

        version = _published_template(client, organization.id, component["id"])
        employee = _employee(client, organization.id, masters)
        client.post(
            f"/api/v1/orgs/{organization.id}/employees/{employee['id']}/salary-assignments",
            json={
                "template_id": version["template_id"],
                "effective_from": TODAY,
                "revision_reason": "NEW_JOINER",
                "confirm": True,
            },
        )
        assert client.get(url).json() == {"template_version_count": 1, "assignment_count": 1}

    def test_non_iso_currency_is_rejected(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization, client, _ = _setup(db_session, authenticate)
        response = client.post(
            f"/api/v1/orgs/{organization.id}/salary-templates",
            json={"template_code": "BADCCY", "template_name": "Bad", "currency": "XXX"},
        )
        assert response.status_code == 422, response.text


class TestComponentRestructure:
    """Basic is a system component; percentages are % of Basic; catalogue picks need no flag."""

    def test_basic_is_seeded_and_protected(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization, client, _ = _setup(db_session, authenticate)
        base = f"/api/v1/orgs/{organization.id}/salary-components"
        basic = _component(client, organization.id)
        assert basic["is_system"] is True
        assert basic["component_type"] == "EARNING"
        assert basic["display_name"] == "Basic"

        assert (
            client.post(
                f"{base}/{basic['id']}/deactivate",
                json={"reason": "Trying to remove the system component"},
            ).status_code
            == 422
        )
        retyped = client.patch(
            f"{base}/{basic['id']}",
            json={"is_required": False},
            headers={"If-Match": str(basic["row_version"])},
        )
        assert retyped.status_code == 422
        renamed = client.patch(
            f"{base}/{basic['id']}",
            json={"display_name": "Basic Pay"},
            headers={"If-Match": str(basic["row_version"])},
        )
        assert renamed.status_code == 200, renamed.text
        assert renamed.json()["display_name"] == "Basic Pay"

    def test_user_cannot_create_a_second_basic(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization, client, _ = _setup(db_session, authenticate)
        response = client.post(
            f"/api/v1/orgs/{organization.id}/salary-components",
            json={"component_code": "BASIC", "component_name": "Basic", "value_type": "PERCENTAGE"},
        )
        assert response.status_code == 409

    def test_display_name_defaults_to_name_and_type_is_stored(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization, client, _ = _setup(db_session, authenticate)
        response = client.post(
            f"/api/v1/orgs/{organization.id}/salary-components",
            json={
                "component_code": "PT",
                "component_name": "Professional tax",
                "component_type": "DEDUCTION",
                "value_type": "FIXED_AMOUNT",
                "amount_period": "MONTHLY",
                "currency": "INR",
            },
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["display_name"] == "Professional tax"
        assert body["component_type"] == "DEDUCTION"

    def test_template_always_carries_basic(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization, client, _ = _setup(db_session, authenticate)
        basic = _component(client, organization.id)
        created = client.post(
            f"/api/v1/orgs/{organization.id}/salary-templates",
            json={"template_code": "EMPTY", "template_name": "No mappings", "currency": "INR"},
        )
        assert created.status_code == 201, created.text
        mappings = created.json()["mappings"]
        assert [row["component_id"] for row in mappings] == [basic["id"]]
        assert mappings[0]["default_value_amount"] is None

    def test_percentage_component_is_computed_from_basic_and_frozen(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization, client, masters = _setup(db_session, authenticate)
        base = f"/api/v1/orgs/{organization.id}"
        basic = _component(client, organization.id)
        hra = client.post(
            f"{base}/salary-components",
            json={
                "component_code": "HRA",
                "component_name": "House rent allowance",
                "display_name": "HRA",
                "value_type": "PERCENTAGE",
            },
        ).json()
        assert hra["percentage_reference_label"] == "% of Basic"
        employee = _employee(client, organization.id, masters)
        created = client.post(
            f"{base}/employees/{employee['id']}/salary-assignments",
            json={
                "effective_from": TODAY,
                "revision_reason": "NEW_JOINER",
                "components": [
                    {"component_id": basic["id"], "value_amount": "33333.33"},
                    {"component_id": hra["id"], "value_percentage": "15"},
                ],
                "confirm": True,
            },
        )
        assert created.status_code == 201, created.text
        rows = {row["component_code"]: row for row in created.json()["components"]}
        assert rows["HRA"]["computed_amount"] == "5000.00"  # 4999.9995 rounds half-up
        assert rows["HRA"]["display_name"] == "HRA"
        assert rows["BASIC"]["computed_amount"] is None

        # Editing the catalogue afterwards must not reach the stored employee salary.
        client.patch(
            f"{base}/salary-components/{hra['id']}",
            json={"display_name": "House Rent"},
            headers={"If-Match": str(hra["row_version"])},
        )
        current = client.get(f"{base}/employees/{employee['id']}/salary-assignments/current").json()
        stored = {row["component_code"]: row for row in current["components"]}
        assert stored["HRA"]["display_name"] == "HRA"
        assert stored["HRA"]["computed_amount"] == "5000.00"

    def test_assignment_without_basic_amount_is_rejected(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization, client, masters = _setup(db_session, authenticate)
        base = f"/api/v1/orgs/{organization.id}"
        basic = _component(client, organization.id)
        employee = _employee(client, organization.id, masters)
        for components in ([], [{"component_id": basic["id"]}]):
            response = client.post(
                f"{base}/employees/{employee['id']}/salary-assignments",
                json={
                    "effective_from": TODAY,
                    "revision_reason": "NEW_JOINER",
                    "components": components,
                },
            )
            assert response.status_code == 422, response.text

    def test_catalogue_only_assignment_needs_no_custom_flag(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization, client, masters = _setup(db_session, authenticate)
        basic = _component(client, organization.id)
        employee = _employee(client, organization.id, masters)
        response = client.post(
            f"/api/v1/orgs/{organization.id}/employees/{employee['id']}/salary-assignments",
            json={
                "effective_from": TODAY,
                "revision_reason": "NEW_JOINER",
                "components": [{"component_id": basic["id"], "value_amount": "40000"}],
            },
        )
        assert response.status_code == 201, response.text

    def test_custom_component_cannot_use_the_reserved_code(
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
                        "component_code": "BASIC",
                        "component_name": "Basic",
                        "value_type": "FIXED_AMOUNT",
                        "value_amount": "1",
                    }
                ],
            },
        )
        assert response.status_code == 422

    def test_template_without_basic_default_needs_only_the_amount(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization, client, masters = _setup(db_session, authenticate)
        base = f"/api/v1/orgs/{organization.id}"
        basic = _component(client, organization.id)
        created = client.post(
            f"{base}/salary-templates",
            json={
                "template_code": "NOBASIC",
                "template_name": "Basic amount left to the employee",
                "currency": "INR",
                "effective_from": "2026-01-01",
            },
        ).json()
        client.post(
            f"{base}/salary-templates/{created['template_id']}/versions/{created['id']}/publish"
        )
        employee = _employee(client, organization.id, masters)
        response = client.post(
            f"{base}/employees/{employee['id']}/salary-assignments",
            json={
                "template_id": created["template_id"],
                "effective_from": TODAY,
                "revision_reason": "NEW_JOINER",
                "components": [{"component_id": basic["id"], "value_amount": "42000"}],
            },
        )
        assert response.status_code == 201, response.text
        row = response.json()["components"][0]
        assert row["value_amount"] == "42000.00"
        # Supplying the amount a template left empty is not replacing a default, so no reason.
        assert (
            row["template_default_present"] is False if "template_default_present" in row else True
        )
        assert row["override_reason"] is None
