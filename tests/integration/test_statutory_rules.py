"""Payroll addendum §4.6 / §15.2: platform rule versions, maker-checker publishing."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from app.core.permissions import Role
from app.db.types import uuid7
from app.models.statutory_rules import StatutoryRuleParameter, StatutoryRuleSet
from tests.factories import create_organization, create_user, unique_suffix

SLABS = [
    {
        "slab_table_code": "RATES",
        "sequence_no": 1,
        "lower_bound": "0.00",
        "upper_bound": "300000.00",
        "rate_pct": "0",
    },
    {"slab_table_code": "RATES", "sequence_no": 2, "lower_bound": "300000.00", "rate_pct": "5"},
]


def _body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "effective_from": "2026-04-01",
        "parameter_schema_version": "PF.v1",
        "parameters": [
            {"parameter_code": "EMPLOYEE_RATE", "value_type": "RATE_PCT", "value": "12"},
            {"parameter_code": "WAGE_CEILING", "value_type": "MONEY", "value": "15000"},
            {"parameter_code": "VPF_ALLOWED", "value_type": "BOOLEAN", "value": True},
        ],
        "slabs": SLABS,
        "list_values": [{"list_code": "EXEMPT", "value_code": "X1", "label": "Exempt one"}],
    }
    body.update(overrides)
    return body


def _rule_set(db_session: Any, family: str = "PF") -> Any:
    row = StatutoryRuleSet(
        id=uuid7(),
        rule_family=family,
        jurisdiction_code="IN",
        rule_set_code=f"R{unique_suffix()[:8]}".upper(),
        status="ACTIVE",
    )
    db_session.add(row)
    db_session.commit()
    return row.id


def _user(db_session: Any, authenticate: Any, role: Role) -> Any:
    organization = create_organization(db_session)
    user = create_user(db_session, grants=[(organization.id, role)])
    db_session.commit()
    return authenticate(user)


def _draft(client: Any, set_id: Any, **overrides: Any) -> Any:
    return client.post(f"/api/v1/statutory-rules/{set_id}/versions", json=_body(**overrides))


def _publish(client: Any, version_id: Any, status: str = "SECONDARY_CONFIRMED") -> Any:
    return client.post(
        f"/api/v1/statutory-rules/versions/{version_id}/publish",
        json={"verification_status": status},
    )


class TestDraft:
    def test_a_draft_stores_typed_values_and_is_not_browsable(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        maker = _user(db_session, authenticate, Role.SYSTEM_SUPER_ADMIN)
        set_id = _rule_set(db_session)
        response = _draft(maker, set_id)
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["status"] == "DRAFT" and body["version_number"] == 1
        assert body["verification_status"] == "UNVERIFIED"
        by_code = {p["parameter_code"]: p for p in body["parameters"]}
        assert Decimal(by_code["EMPLOYEE_RATE"]["value_decimal"]) == 12
        assert by_code["VPF_ALLOWED"]["value_boolean"] is True
        assert _draft(maker, set_id).json()["version_number"] == 2

        reader = _user(db_session, authenticate, Role.PAYROLL_ADMINISTRATOR)
        listing = reader.get("/api/v1/statutory-rules", params={"family": "PF"}).json()
        mine = next(item for item in listing if item["id"] == str(set_id))
        assert mine["versions"] == []

    def test_bad_input_is_refused(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        maker = _user(db_session, authenticate, Role.SYSTEM_SUPER_ADMIN)
        set_id = _rule_set(db_session)
        assert _draft(maker, set_id, parameter_schema_version="ESI.v1").status_code == 422
        assert _draft(maker, set_id, effective_to="2026-04-01").status_code == 422
        number = {"parameter_code": "X", "value_type": "MONEY", "value": 15000}
        assert _draft(maker, set_id, parameters=[number]).status_code == 422
        junk = {"parameter_code": "X", "value_type": "MONEY", "value": "abc"}
        assert _draft(maker, set_id, parameters=[junk]).status_code == 422
        flag = {"parameter_code": "X", "value_type": "BOOLEAN", "value": "yes"}
        assert _draft(maker, set_id, parameters=[flag]).status_code == 422
        twice = [SLABS[0], SLABS[0]]
        assert _draft(maker, set_id, slabs=twice).status_code == 422
        no_amount = [{**SLABS[0], "rate_pct": None}]
        assert _draft(maker, set_id, slabs=no_amount).status_code == 422
        assert _draft(maker, uuid7()).status_code == 404

    def test_only_the_platform_admin_may_draft(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        set_id = _rule_set(db_session)
        for role in (Role.PAYROLL_ADMINISTRATOR, Role.STATUTORY_COMPLIANCE_OFFICER):
            assert _draft(_user(db_session, authenticate, role), set_id).status_code == 403


class TestPublish:
    def test_the_preparer_cannot_publish(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        maker = _user(db_session, authenticate, Role.SYSTEM_SUPER_ADMIN)
        version = _draft(maker, _rule_set(db_session)).json()
        response = _publish(maker, version["id"])
        assert response.status_code == 403, response.text
        assert response.json()["error"]["code"] == "RULE_SELF_PUBLISH_NOT_ALLOWED"

    def test_publishing_by_another_admin_makes_it_resolvable(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        maker = _user(db_session, authenticate, Role.SYSTEM_SUPER_ADMIN)
        checker = _user(db_session, authenticate, Role.SYSTEM_SUPER_ADMIN)
        set_id = _rule_set(db_session)
        version = _draft(maker, set_id).json()
        published = _publish(checker, version["id"], "PRIMARY_CONFIRMED")
        assert published.status_code == 200, published.text
        body = published.json()
        assert body["status"] == "PUBLISHED" and body["verification_status"] == "PRIMARY_CONFIRMED"
        assert body["published_by"] != body["prepared_by"]
        assert _publish(checker, version["id"]).status_code == 422  # no longer a draft

        reader = _user(db_session, authenticate, Role.PAYROLL_ADMINISTRATOR)

        def versions_on(day: str) -> list[Any]:
            response = reader.get("/api/v1/statutory-rules", params={"family": "PF", "as_of": day})
            mine = next(i for i in response.json() if i["id"] == str(set_id))
            return mine["versions"]

        assert versions_on("2026-03-31") == []
        in_force = versions_on("2026-04-01")
        assert [v["id"] for v in in_force] == [version["id"]]
        assert len(in_force[0]["slabs"]) == 2 and len(in_force[0]["list_values"]) == 1

    def test_publishing_a_successor_closes_the_predecessor(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        maker = _user(db_session, authenticate, Role.SYSTEM_SUPER_ADMIN)
        checker = _user(db_session, authenticate, Role.SYSTEM_SUPER_ADMIN)
        set_id = _rule_set(db_session)
        first = _draft(maker, set_id).json()
        _publish(checker, first["id"])
        second = _draft(maker, set_id, effective_from="2027-04-01").json()
        assert _publish(checker, second["id"]).status_code == 200

        reader = _user(db_session, authenticate, Role.PAYROLL_ADMINISTRATOR)
        listing = reader.get("/api/v1/statutory-rules", params={"family": "PF"}).json()
        versions = next(i for i in listing if i["id"] == str(set_id))["versions"]
        by_number = {v["version_number"]: v for v in versions}
        assert by_number[1]["effective_to"] == "2027-04-01"
        assert by_number[2]["effective_to"] is None

    def test_a_version_cannot_start_before_published_history(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        maker = _user(db_session, authenticate, Role.SYSTEM_SUPER_ADMIN)
        checker = _user(db_session, authenticate, Role.SYSTEM_SUPER_ADMIN)
        set_id = _rule_set(db_session)
        _publish(checker, _draft(maker, set_id).json()["id"])
        earlier = _draft(maker, set_id, effective_from="2025-04-01").json()
        refused = _publish(checker, earlier["id"])
        assert refused.status_code == 422, refused.text

    def test_non_contiguous_slabs_block_publication(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        maker = _user(db_session, authenticate, Role.SYSTEM_SUPER_ADMIN)
        checker = _user(db_session, authenticate, Role.SYSTEM_SUPER_ADMIN)
        gap = [SLABS[0], {**SLABS[1], "lower_bound": "400000.00"}]
        draft = _draft(maker, _rule_set(db_session), slabs=gap)
        assert draft.status_code == 201
        refused = _publish(checker, draft.json()["id"])
        assert refused.status_code == 422, refused.text
        assert "SLAB_INVALID" in refused.text

    def test_only_the_platform_admin_may_publish(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        maker = _user(db_session, authenticate, Role.SYSTEM_SUPER_ADMIN)
        version = _draft(maker, _rule_set(db_session)).json()
        other = _user(db_session, authenticate, Role.STATUTORY_COMPLIANCE_OFFICER)
        assert _publish(other, version["id"]).status_code == 403
        assert (
            _publish(_user(db_session, authenticate, Role.SYSTEM_SUPER_ADMIN), uuid7()).status_code
            == 404
        )


class TestStorage:
    def test_each_parameter_row_holds_exactly_one_value(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        maker = _user(db_session, authenticate, Role.SYSTEM_SUPER_ADMIN)
        version = _draft(maker, _rule_set(db_session)).json()
        rows = db_session.execute(
            select(StatutoryRuleParameter).where(
                StatutoryRuleParameter.rule_version_id == uuid.UUID(version["id"])
            )
        ).scalars()
        for row in rows:
            held = [row.value_decimal, row.value_text, row.value_boolean, row.value_date]
            assert sum(value is not None for value in held) == 1
