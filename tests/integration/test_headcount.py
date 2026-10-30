"""Headcount plans, change requests and reports (JA §21.3).

Maker-checker needs two people, so most of these tests authenticate a second user: the
specification is explicit that an organization with only one eligible approver cannot approve
anything, and that the remedy is access administration rather than an exception.
"""

from __future__ import annotations

from typing import Any

from app.core.errors import ErrorCode
from app.core.permissions import Role
from tests.factories import create_user
from tests.job_architecture import idempotent, if_match, setup_architecture


def _approver(arch: Any, db_session: Any, authenticate: Any) -> Any:
    """A second authenticated client that may approve what the first user created."""
    user = create_user(db_session, grants=[(arch.organization.id, Role.ORGANIZATION_ADMIN)])
    db_session.commit()
    return authenticate(user)


def _change_request(arch: Any, **overrides: Any) -> dict[str, Any]:
    body = {
        "change_type": "INCREASE",
        "requested_fte": "2.00",
        "requested_headcount": 2,
        "reason": "Approved growth in the platform team for the next quarter",
    }
    body.update(overrides)
    response = arch.client.post(
        f"{arch.base}/headcount-change-requests", json=body, headers=idempotent()
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


class TestHeadcountChangeRequests:
    def test_approval_applies_the_change_and_blocks_self_approval(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        """AC-JA-15: the creator cannot approve; a different approver can, and it applies."""
        arch = setup_architecture(db_session, authenticate)
        position = arch.open_new_position(authorized_fte="1.00", max_incumbents=1)
        request = _change_request(arch, position_id=position["id"])
        assert request["current_fte"] == "1.00"
        assert request["request_status"] == "DRAFT"

        submitted = arch.client.post(
            f"{arch.base}/headcount-change-requests/{request['id']}/submit",
            headers=if_match(request),
        )
        assert submitted.status_code == 200, submitted.text
        assert submitted.json()["request_status"] == "PENDING"

        self_approval = arch.client.post(
            f"{arch.base}/headcount-change-requests/{request['id']}/approve",
            json={"note": "Approving my own request"},
            headers=if_match(submitted.json()),
        )
        assert self_approval.status_code == 403, self_approval.text
        assert self_approval.json()["error"]["code"] == ErrorCode.SELF_APPROVAL_NOT_ALLOWED.value

        approver = _approver(arch, db_session, authenticate)
        approved = approver.post(
            f"{arch.base}/headcount-change-requests/{request['id']}/approve",
            json={"note": "Confirmed against the approved plan"},
            headers=if_match(submitted.json()),
        )
        assert approved.status_code == 200, approved.text
        assert approved.json()["request_status"] == "APPROVED"
        assert approved.json()["applied_at"] is not None

        updated = arch.reload_position(position["id"])
        assert updated["authorized_fte"] == "2.00"
        assert updated["max_incumbents"] == 2

        audit = arch.client.get(
            f"{arch.base}/audit-logs", params={"entity_type": "headcount_change_request"}
        )
        actions = {row["action"] for row in audit.json()}
        assert {"HEADCOUNT_REQUEST_APPROVED", "HEADCOUNT_REQUEST_APPLIED"} <= actions

    def test_a_decrease_below_occupancy_is_rejected(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        """AC-JA-16: ``HEADCOUNT_BELOW_OCCUPIED``; the position keeps its authorization."""
        arch = setup_architecture(db_session, authenticate)
        position = arch.open_new_position(authorized_fte="2.00", max_incumbents=2)
        first = arch.create_employee()
        second = arch.create_employee()
        assert arch.fill(first, position, fte="1.00").status_code == 200
        assert arch.fill(second, position, fte="0.50").status_code == 200

        request = _change_request(
            arch,
            position_id=position["id"],
            change_type="DECREASE",
            requested_fte="1.00",
            requested_headcount=1,
        )
        submitted = arch.client.post(
            f"{arch.base}/headcount-change-requests/{request['id']}/submit",
            headers=if_match(request),
        )
        assert submitted.status_code == 200, submitted.text

        approver = _approver(arch, db_session, authenticate)
        rejected = approver.post(
            f"{arch.base}/headcount-change-requests/{request['id']}/approve",
            json={"note": "Trying to shrink below the current occupancy"},
            headers=if_match(submitted.json()),
        )
        assert rejected.status_code == 422, rejected.text
        assert rejected.json()["error"]["code"] == ErrorCode.HEADCOUNT_BELOW_OCCUPIED.value

        assert arch.reload_position(position["id"])["authorized_fte"] == "2.00"
        still_pending = arch.client.get(f"{arch.base}/headcount-change-requests/{request['id']}")
        assert still_pending.json()["request_status"] == "PENDING"

    def test_an_increase_cannot_request_less(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        """JA §8.7: the change type and the requested values must agree."""
        arch = setup_architecture(db_session, authenticate)
        position = arch.open_new_position(authorized_fte="2.00", max_incumbents=2)
        response = arch.client.post(
            f"{arch.base}/headcount-change-requests",
            json={
                "position_id": position["id"],
                "change_type": "INCREASE",
                "requested_fte": "1.00",
                "requested_headcount": 1,
                "reason": "Mislabelled as an increase when it is a reduction",
            },
            headers=idempotent(),
        )
        assert response.status_code == 422, response.text

    def test_exactly_one_target_is_required(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        arch = setup_architecture(db_session, authenticate)
        position = arch.open_new_position()
        plan = arch.create_plan()
        neither = arch.client.post(
            f"{arch.base}/headcount-change-requests",
            json={
                "change_type": "SET",
                "requested_fte": "1.00",
                "requested_headcount": 1,
                "reason": "A request with no target at all whatsoever",
            },
            headers=idempotent(),
        )
        assert neither.status_code == 422, neither.text

        both = arch.client.post(
            f"{arch.base}/headcount-change-requests",
            json={
                "position_id": position["id"],
                "headcount_plan_id": plan["id"],
                "change_type": "SET",
                "requested_fte": "1.00",
                "requested_headcount": 1,
                "reason": "A request pointing at two different targets",
            },
            headers=idempotent(),
        )
        assert both.status_code == 422, both.text

    def test_a_short_reason_is_rejected(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        """JA §8.7: at least twenty characters."""
        arch = setup_architecture(db_session, authenticate)
        position = arch.open_new_position()
        response = arch.client.post(
            f"{arch.base}/headcount-change-requests",
            json={
                "position_id": position["id"],
                "change_type": "INCREASE",
                "requested_fte": "2.00",
                "requested_headcount": 2,
                "reason": "too short",
            },
            headers=idempotent(),
        )
        assert response.status_code == 422

    def test_rejection_requires_a_note_and_is_terminal(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        arch = setup_architecture(db_session, authenticate)
        position = arch.open_new_position()
        request = _change_request(arch, position_id=position["id"])
        submitted = arch.client.post(
            f"{arch.base}/headcount-change-requests/{request['id']}/submit",
            headers=if_match(request),
        )
        approver = _approver(arch, db_session, authenticate)

        without_note = approver.post(
            f"{arch.base}/headcount-change-requests/{request['id']}/reject",
            json={},
            headers=if_match(submitted.json()),
        )
        assert without_note.status_code == 422, without_note.text

        rejected = approver.post(
            f"{arch.base}/headcount-change-requests/{request['id']}/reject",
            json={"note": "Not funded in this quarter's plan"},
            headers=if_match(submitted.json()),
        )
        assert rejected.status_code == 200, rejected.text
        assert rejected.json()["request_status"] == "REJECTED"

        again = approver.post(
            f"{arch.base}/headcount-change-requests/{request['id']}/approve",
            json={"note": "Changing my mind after rejecting"},
            headers=if_match(rejected.json()),
        )
        assert again.status_code == 422
        assert again.json()["error"]["code"] == ErrorCode.HEADCOUNT_CHANGE_NOT_APPROVABLE.value

    def test_a_non_draft_request_cannot_be_edited(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        arch = setup_architecture(db_session, authenticate)
        position = arch.open_new_position()
        request = _change_request(arch, position_id=position["id"])
        submitted = arch.client.post(
            f"{arch.base}/headcount-change-requests/{request['id']}/submit",
            headers=if_match(request),
        )
        response = arch.client.patch(
            f"{arch.base}/headcount-change-requests/{request['id']}",
            json={"requested_fte": "3.00"},
            headers=if_match(submitted.json()),
        )
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == ErrorCode.HEADCOUNT_CHANGE_NOT_EDITABLE.value

    def test_a_request_can_be_cancelled(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        arch = setup_architecture(db_session, authenticate)
        position = arch.open_new_position()
        request = _change_request(arch, position_id=position["id"])
        cancelled = arch.client.post(
            f"{arch.base}/headcount-change-requests/{request['id']}/cancel",
            json={"reason": "Superseded by the revised plan"},
            headers=if_match(request),
        )
        assert cancelled.status_code == 200, cancelled.text
        assert cancelled.json()["request_status"] == "CANCELLED"


class TestHeadcountPlans:
    def test_activation_needs_a_second_person(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        """JA §15.4: a plan is only a cap once somebody other than its creator approves it."""
        arch = setup_architecture(db_session, authenticate)
        plan = arch.create_plan()
        assert plan["plan_status"] == "DRAFT"

        own = arch.client.post(
            f"{arch.base}/headcount-plans/{plan['id']}/activate",
            json={"note": "Activating my own plan"},
            headers=if_match(plan),
        )
        assert own.status_code == 403, own.text
        assert own.json()["error"]["code"] == ErrorCode.SELF_APPROVAL_NOT_ALLOWED.value

        approver = _approver(arch, db_session, authenticate)
        activated = approver.post(
            f"{arch.base}/headcount-plans/{plan['id']}/activate",
            json={"note": "Reviewed against the budget"},
            headers=if_match(plan),
        )
        assert activated.status_code == 200, activated.text
        assert activated.json()["plan_status"] == "ACTIVE"
        assert activated.json()["approved_by"] is not None

    def test_a_plan_caps_opening_further_positions(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        """AC-JA-14: opening beyond the cap is ``HEADCOUNT_LIMIT_EXCEEDED``."""
        arch = setup_architecture(db_session, authenticate)
        approver = _approver(arch, db_session, authenticate)
        plan = arch.create_plan(authorized_fte="2.00", authorized_headcount=2)
        activated = approver.post(
            f"{arch.base}/headcount-plans/{plan['id']}/activate",
            json={"note": "Department establishment for the year"},
            headers=if_match(plan),
        )
        assert activated.status_code == 200, activated.text

        arch.open_new_position(position_code="P-1")
        arch.open_new_position(position_code="P-2")

        third = arch.create_position(position_code="P-3")
        blocked = arch.client.post(
            f"{arch.base}/positions/{third['id']}/open",
            json={"reason": "Opening beyond the department plan"},
            headers=if_match(third),
        )
        assert blocked.status_code == 422, blocked.text
        assert blocked.json()["error"]["code"] == ErrorCode.HEADCOUNT_LIMIT_EXCEEDED.value
        detail = blocked.json()["error"]["details"][0]["message"]
        assert "2.00" in detail

    def test_a_plan_cannot_be_reduced_below_its_establishment(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        """AC-JA-14: ``HEADCOUNT_BELOW_AUTHORIZED`` — close positions first."""
        arch = setup_architecture(db_session, authenticate)
        approver = _approver(arch, db_session, authenticate)
        plan = arch.create_plan(authorized_fte="2.00", authorized_headcount=2)
        approver.post(
            f"{arch.base}/headcount-plans/{plan['id']}/activate",
            json={"note": "Department establishment for the year"},
            headers=if_match(plan),
        )
        arch.open_new_position(position_code="HELD-1")
        arch.open_new_position(position_code="HELD-2")

        request = _change_request(
            arch,
            headcount_plan_id=plan["id"],
            change_type="DECREASE",
            requested_fte="1.50",
            requested_headcount=2,
        )
        submitted = arch.client.post(
            f"{arch.base}/headcount-change-requests/{request['id']}/submit",
            headers=if_match(request),
        )
        assert submitted.status_code == 200, submitted.text
        response = approver.post(
            f"{arch.base}/headcount-change-requests/{request['id']}/approve",
            json={"note": "Reducing the plan below what is already authorized"},
            headers=if_match(submitted.json()),
        )
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == ErrorCode.HEADCOUNT_BELOW_AUTHORIZED.value

    def test_overlapping_active_plans_for_one_scope_are_rejected(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        """JA §8.6: one active plan per exact scope and period."""
        arch = setup_architecture(db_session, authenticate)
        approver = _approver(arch, db_session, authenticate)
        first = arch.create_plan(plan_code="PLAN-A", effective_from="2026-01-01")
        approver.post(
            f"{arch.base}/headcount-plans/{first['id']}/activate",
            json={"note": "First plan for the department"},
            headers=if_match(first),
        )
        second = arch.create_plan(plan_code="PLAN-B", effective_from="2026-06-01")
        response = approver.post(
            f"{arch.base}/headcount-plans/{second['id']}/activate",
            json={"note": "Second overlapping plan"},
            headers=if_match(second),
        )
        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == ErrorCode.HEADCOUNT_PLAN_OVERLAP.value

    def test_a_plan_requires_exactly_one_scope_id(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        arch = setup_architecture(db_session, authenticate)
        missing = arch.client.post(
            f"{arch.base}/headcount-plans",
            json={
                "plan_code": "NO-SCOPE",
                "name": "Department plan without a department",
                "scope_type": "DEPARTMENT",
                "authorized_fte": "1.00",
                "authorized_headcount": 1,
                "effective_from": "2026-01-01",
            },
            headers=idempotent(),
        )
        assert missing.status_code == 422, missing.text

        extra = arch.client.post(
            f"{arch.base}/headcount-plans",
            json={
                "plan_code": "TWO-SCOPES",
                "name": "Organization plan with a department",
                "scope_type": "ORGANIZATION",
                "department_id": arch.ids["department_id"],
                "authorized_fte": "1.00",
                "authorized_headcount": 1,
                "effective_from": "2026-01-01",
            },
            headers=idempotent(),
        )
        assert extra.status_code == 422, extra.text

    def test_plan_read_reports_the_committed_establishment(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        """JA §8.4 ``PlanAuthorizedFTE`` and ``PlanAvailableFTE`` on a single-plan read."""
        arch = setup_architecture(db_session, authenticate)
        approver = _approver(arch, db_session, authenticate)
        plan = arch.create_plan(authorized_fte="3.00", authorized_headcount=3)
        approver.post(
            f"{arch.base}/headcount-plans/{plan['id']}/activate",
            json={"note": "Establishment for the department"},
            headers=if_match(plan),
        )
        arch.open_new_position(position_code="COUNTED")

        response = arch.client.get(f"{arch.base}/headcount-plans/{plan['id']}")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["plan_authorized_fte"] == "1.00"
        assert body["plan_available_fte"] == "2.00"
        assert body["plan_authorized_headcount"] == 1
        assert body["plan_available_headcount"] == 2

    def test_a_draft_plan_does_not_cap_anything(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        """JA §8.6: ``DRAFT`` plans are not enforced."""
        arch = setup_architecture(db_session, authenticate)
        arch.create_plan(authorized_fte="0.00", authorized_headcount=0)
        position = arch.create_position()
        opened = arch.client.post(
            f"{arch.base}/positions/{position['id']}/open",
            json={"reason": "A draft plan must not block this"},
            headers=if_match(position),
        )
        assert opened.status_code == 200, opened.text


class TestHeadcountReports:
    def test_summary_groups_and_excludes_personal_data(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        """AC-JA-14 / AC-JA-18: counts by department, with no employee data anywhere."""
        arch = setup_architecture(db_session, authenticate)
        position = arch.open_new_position(authorized_fte="2.00", max_incumbents=2)
        employee = arch.create_employee()
        assert arch.fill(employee, position, fte="1.00").status_code == 200

        response = arch.client.get(
            f"{arch.base}/headcount/summary", params={"group_by": "DEPARTMENT"}
        )
        assert response.status_code == 200, response.text
        rows = response.json()["items"]
        assert len(rows) == 1
        row = rows[0]
        assert row["authorized_fte"] == "2.00"
        assert row["occupied_fte"] == "1.00"
        assert row["vacant_fte"] == "1.00"
        assert row["authorized_headcount"] == 2
        assert row["occupied_headcount"] == 1
        assert row["vacant_headcount"] == 1
        assert row["over_capacity"] == 0
        assert row["group_label"] == "Engineering"

        serialised = response.text
        assert employee["first_name"] not in serialised
        assert employee["employee_code"] not in serialised

    def test_organization_grouping_and_draft_exclusion(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        """JA §17.6: ``DRAFT`` positions are excluded unless the planning view asks for them."""
        arch = setup_architecture(db_session, authenticate)
        arch.open_new_position(position_code="OPEN-1")
        arch.create_position(position_code="DRAFT-1")

        default = arch.client.get(f"{arch.base}/headcount/summary")
        assert default.status_code == 200, default.text
        assert default.json()["items"][0]["authorized_fte"] == "1.00"

        planning = arch.client.get(f"{arch.base}/headcount/summary", params={"include_draft": True})
        assert planning.json()["items"][0]["authorized_fte"] == "2.00"

    def test_closed_positions_leave_the_totals(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        arch = setup_architecture(db_session, authenticate)
        position = arch.open_new_position()
        closed = arch.client.post(
            f"{arch.base}/positions/{position['id']}/close",
            json={"reason": "Removed from the establishment"},
            headers=if_match(position),
        )
        assert closed.status_code == 200, closed.text
        summary = arch.client.get(f"{arch.base}/headcount/summary")
        assert summary.json()["items"][0]["authorized_fte"] == "0.00"

    def test_unknown_group_by_is_rejected(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        arch = setup_architecture(db_session, authenticate)
        response = arch.client.get(
            f"{arch.base}/headcount/summary", params={"group_by": "SALARY_BAND"}
        )
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == ErrorCode.VALIDATION_FAILED.value

    def test_reconciliation_is_empty_when_everything_is_consistent(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        """JA §17.5: an empty findings list is the expected, healthy result."""
        arch = setup_architecture(db_session, authenticate)
        position = arch.open_new_position()
        employee = arch.create_employee()
        assert arch.fill(employee, position).status_code == 200

        response = arch.client.get(f"{arch.base}/headcount/reconciliation")
        assert response.status_code == 200, response.text
        assert response.json()["findings"] == []
        assert response.json()["as_of"]

    def test_summary_and_single_position_headcount_agree(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        """JA §17.6: one set of formulas, so the two reports cannot disagree."""
        arch = setup_architecture(db_session, authenticate)
        position = arch.open_new_position(authorized_fte="2.00", max_incumbents=2)
        employee = arch.create_employee()
        arch.fill(employee, position, fte="0.50")

        by_position = arch.client.get(
            f"{arch.base}/headcount/summary", params={"group_by": "POSITION"}
        ).json()["items"][0]
        single = arch.headcount(position)["occupancy"]
        assert by_position["occupied_fte"] == single["occupied_fte"]
        assert by_position["vacant_fte"] == single["vacant_fte"]
        assert by_position["occupied_headcount"] == single["occupied_headcount"]


class TestReservations:
    def test_reservations_are_disabled_by_default(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        """JA-Q-14: off until an onboarding or recruitment integration can release them."""
        arch = setup_architecture(db_session, authenticate)
        position = arch.open_new_position()
        response = arch.client.post(
            f"{arch.base}/positions/{position['id']}/headcount-reservations",
            json={
                "reserved_fte": "1.00",
                "reserved_headcount": 1,
                "source_type": "OFFER",
                "expires_at": "2099-01-01T00:00:00Z",
            },
            headers=idempotent(),
        )
        assert response.status_code == 422, response.text
        assert response.json()["error"]["details"][0]["code"] == "DISABLED"

    def test_an_enabled_reservation_reduces_and_restores_vacancy(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        """AC-JA-12: a reservation consumes capacity, and releasing it gives it back."""
        arch = setup_architecture(db_session, authenticate)
        settings = arch.client.get(f"{arch.base}/settings").json()
        settings.pop("organization_id", None)
        settings["enable_headcount_reservations"] = True
        updated = arch.client.put(f"{arch.base}/settings", json=settings)
        assert updated.status_code == 200, updated.text

        position = arch.open_new_position()
        created = arch.client.post(
            f"{arch.base}/positions/{position['id']}/headcount-reservations",
            json={
                "reserved_fte": "1.00",
                "reserved_headcount": 1,
                "source_type": "OFFER",
                "source_reference": "REQ-1024",
                "expires_at": "2099-01-01T00:00:00Z",
            },
            headers=idempotent(),
        )
        assert created.status_code == 201, created.text

        occupancy = arch.headcount(position)["occupancy"]
        assert occupancy["reserved_fte"] == "1.00"
        assert occupancy["vacant_fte"] == "0.00"
        assert occupancy["is_fillable"] is False

        employee = arch.create_employee()
        blocked = arch.fill(employee, position)
        assert blocked.status_code == 422
        assert blocked.json()["error"]["code"] == ErrorCode.POSITION_HEADCOUNT_EXCEEDED.value

        released = arch.client.post(
            f"{arch.base}/positions/{position['id']}"
            f"/headcount-reservations/{created.json()['id']}/release",
            json={"reason": "The candidate declined the offer"},
            headers=if_match(created.json()),
        )
        assert released.status_code == 200, released.text
        assert released.json()["reservation_status"] == "RELEASED"

        restored = arch.headcount(position)["occupancy"]
        assert restored["reserved_fte"] == "0.00"
        assert restored["vacant_fte"] == "1.00"
        assert arch.fill(employee, position).status_code == 200
