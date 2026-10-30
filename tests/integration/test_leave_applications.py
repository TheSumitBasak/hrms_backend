"""Leave applications: validation, reservation, approval, cancellation (addendum §6.8-§6.12)."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

from app.core.errors import ErrorCode
from app.core.permissions import Role
from tests.factories import create_user
from tests.job_architecture import if_match
from tests.leave_support import LeaveWorld, make_world, next_weekday, working_day


def _code(response: Any) -> str:
    return str(response.json()["error"]["code"])


class Fixture:
    """A world with CL leave, an employee with a manager, and 10 days of opening balance."""

    def __init__(self, db: Any, authenticate: Any, **policy: Any) -> None:
        self.w: LeaveWorld = make_world(db, authenticate)
        self.leave_type = self.w.leave_type("CL")
        self.w.policy(self.leave_type, **policy)
        self.manager = self.w.employee(first_name="Mira")
        self.employee = self.w.employee(first_name="Esha")
        self.w.set_manager(self.employee["id"], self.manager["id"])
        self.manager_client = self.w.manager_client(self.manager)
        self.client = self.w.self_client(self.employee)
        self.w.opening(self.employee, self.leave_type, "10.00")

    def apply(self, start: Any, end: Any = None, **extra: Any) -> Any:
        return self.w.apply(self.client, self.leave_type, start, end, **extra)

    def balance(self) -> dict[str, Any]:
        return self.w.balance(self.employee, self.leave_type)

    def act(self, client: Any, application: dict[str, Any], verb: str, body: Any = None) -> Any:
        return client.post(
            f"{self.w.base}/leave-applications/{application['id']}/{verb}",
            json=body or {},
            headers=if_match(application),
        )

    def fetch(self, application_id: str) -> dict[str, Any]:
        response = self.w.hr.get(f"{self.w.base}/leave-applications/{application_id}")
        assert response.status_code == 200, response.text
        return dict(response.json())


class TestDurationAndValidation:
    def test_preview_matches_the_spec_worked_example(
        self, db_session: Any, authenticate: Any
    ) -> None:
        f = Fixture(db_session, authenticate)
        friday = next_weekday(5)
        monday = friday + timedelta(days=3)
        body = {
            "leave_type_id": f.leave_type["id"],
            "start_date": friday.isoformat(),
            "start_half": "FIRST_HALF",
            "end_date": monday.isoformat(),
            "end_half": "FIRST_HALF",
        }
        preview = f.client.post(f"{f.w.base}/leave-applications/preview", json=body)
        assert preview.status_code == 200, preview.text
        data = preview.json()
        assert data["requested_days"] == "1.50"
        kinds = [(s["slot_date"], s["half"], s["counted"]) for s in data["slots"]]
        assert len(kinds) == 7 and sum(1 for k in kinds if k[2]) == 3
        assert data["available_after"] == "8.50"
        # A preview writes nothing.
        assert f.balance()["reserved_days"] == "0.00"

    def test_boundary_on_a_weekly_off_is_refused(self, db_session: Any, authenticate: Any) -> None:
        f = Fixture(db_session, authenticate)
        saturday = next_weekday(6)
        response = f.apply(saturday, saturday + timedelta(days=2))
        assert response.status_code == 422
        assert _code(response) == ErrorCode.LEAVE_BOUNDARY_ON_NON_WORKING_DAY.value

    def test_counting_weekly_offs_adds_them_to_the_request(
        self, db_session: Any, authenticate: Any
    ) -> None:
        f = Fixture(db_session, authenticate, count_weekly_offs=True)
        friday = next_weekday(5)
        response = f.apply(friday, friday + timedelta(days=3), end_half="FIRST_HALF")
        assert response.status_code == 201, response.text
        assert response.json()["requested_days"] == "3.50"

    def test_insufficient_balance_writes_nothing(self, db_session: Any, authenticate: Any) -> None:
        f = Fixture(db_session, authenticate)
        start = working_day(10)
        too_long = f.apply(start, working_day(40))
        assert too_long.status_code == 422
        assert _code(too_long) == ErrorCode.INSUFFICIENT_LEAVE_BALANCE.value
        assert f.balance()["reserved_days"] == "0.00"
        listing = f.client.get(f"{f.w.base}/me/leave-applications").json()
        assert listing["items"] == []

    def test_date_range_and_year_rules(self, db_session: Any, authenticate: Any) -> None:
        f = Fixture(db_session, authenticate)
        start = working_day(5)
        backwards = f.apply(start, start - timedelta(days=1))
        assert _code(backwards) == ErrorCode.INVALID_LEAVE_DATE_RANGE.value
        same_day_reversed = f.apply(start, start, start_half="SECOND_HALF", end_half="FIRST_HALF")
        assert _code(same_day_reversed) == ErrorCode.INVALID_LEAVE_DATE_RANGE.value

    def test_half_day_counts_half(self, db_session: Any, authenticate: Any) -> None:
        f = Fixture(db_session, authenticate)
        day = working_day(5)
        response = f.apply(day, day, end_half="FIRST_HALF")
        assert response.status_code == 201, response.text
        assert response.json()["requested_days"] == "0.50"

    def test_half_days_refused_when_the_policy_forbids_them(
        self, db_session: Any, authenticate: Any
    ) -> None:
        f = Fixture(db_session, authenticate, allow_half_day=False)
        day = working_day(5)
        response = f.apply(day, day, end_half="FIRST_HALF")
        assert _code(response) == ErrorCode.HALF_DAY_NOT_ALLOWED.value

    def test_notice_and_backdate_limits(self, db_session: Any, authenticate: Any) -> None:
        f = Fixture(db_session, authenticate, min_notice_days=5, max_backdate_days=0)
        soon = working_day(1)
        assert _code(f.apply(soon)) == ErrorCode.LEAVE_TIMING_VIOLATION.value
        past = working_day(-3)
        assert _code(f.apply(past)) == ErrorCode.LEAVE_TIMING_VIOLATION.value

    def test_overlapping_application_is_refused(self, db_session: Any, authenticate: Any) -> None:
        f = Fixture(db_session, authenticate)
        day = working_day(6)
        assert f.apply(day).status_code == 201
        second = f.apply(day, day, end_half="FIRST_HALF")
        assert second.status_code == 409
        assert _code(second) == ErrorCode.LEAVE_OVERLAP.value

    def test_supporting_document_and_application_limits(
        self, db_session: Any, authenticate: Any
    ) -> None:
        f = Fixture(
            db_session,
            authenticate,
            document_required_above_days="1.00",
            max_applications_per_year=1,
        )
        day = working_day(6)
        assert (
            _code(f.apply(day, day + timedelta(days=1))) == ErrorCode.LEAVE_DOCUMENT_REQUIRED.value
        )
        assert f.apply(day).status_code == 201
        second = f.apply(working_day(12))
        assert _code(second) == ErrorCode.LEAVE_APPLICATION_LIMIT_EXCEEDED.value

    def test_consecutive_limit_counts_adjacent_applications(
        self, db_session: Any, authenticate: Any
    ) -> None:
        f = Fixture(db_session, authenticate, max_consecutive_days="3.00")
        friday = next_weekday(5)
        assert f.apply(friday).status_code == 201  # 1.00
        monday = friday + timedelta(days=3)  # adjacent across the weekend off
        response = f.apply(monday, monday + timedelta(days=2))  # 3.00 -> chain of 4.00
        assert _code(response) == ErrorCode.LEAVE_CONSECUTIVE_LIMIT_EXCEEDED.value

    def test_negative_balance_within_the_limit_needs_acknowledgement(
        self, db_session: Any, authenticate: Any
    ) -> None:
        f = Fixture(
            db_session, authenticate, allow_negative_balance=True, max_negative_balance_days="3.00"
        )
        start = next_weekday(1, after=8)
        end = start + timedelta(days=14)  # 11 working days against a 10.00 balance
        refused = f.apply(start, end)
        assert refused.status_code == 422
        assert _code(refused) == ErrorCode.WARNINGS_REQUIRE_ACKNOWLEDGEMENT.value
        accepted = f.apply(start, end, acknowledged_warnings=["W-LEAVE-NEGATIVE-BALANCE"])
        assert accepted.status_code == 201, accepted.text
        assert accepted.json()["balance"]["available_days"] == "-1.00"


class TestApprovalFlow:
    def test_submit_reserves_then_approval_consumes(
        self, db_session: Any, authenticate: Any
    ) -> None:
        f = Fixture(db_session, authenticate)
        start = working_day(6)
        submitted = f.apply(start, start, end_half="SECOND_HALF")
        assert submitted.status_code == 201, submitted.text
        application = submitted.json()
        assert application["current_approver_type"] == "MANAGER"
        assert application["balance"]["reserved_days"] == "1.00"
        assert application["balance"]["available_days"] == "9.00"

        approved = f.act(f.manager_client, application, "approve", {"comment": "Fine"})
        assert approved.status_code == 200, approved.text
        body = approved.json()
        assert body["status"] == "APPROVED" and body["consumed_days"] == "1.00"
        assert body["balance"]["posted_balance_days"] == "9.00"
        assert body["balance"]["reserved_days"] == "0.00"
        assert approved.headers["ETag"] == f'"{body["row_version"]}"' or approved.headers["ETag"]

        ledger = f.w.hr.get(f"{f.w.base}/employees/{f.employee['id']}/leave-transactions").json()
        kinds = [t["transaction_type"] for t in ledger["items"]]
        assert kinds == ["OPENING_BALANCE", "CONSUMPTION"]
        again = f.act(f.manager_client, body, "approve")
        assert _code(again) == ErrorCode.INVALID_APPROVAL_TRANSITION.value

    def test_reject_releases_the_reservation_and_the_dates(
        self, db_session: Any, authenticate: Any
    ) -> None:
        f = Fixture(db_session, authenticate)
        day = working_day(6)
        application = f.apply(day).json()
        rejected = f.act(
            f.manager_client,
            application,
            "reject",
            {"decision_comment": "Release is frozen this week"},
        )
        assert rejected.status_code == 200, rejected.text
        assert rejected.json()["status"] == "REJECTED"
        assert f.balance()["reserved_days"] == "0.00"
        assert f.apply(day).status_code == 201  # slots were RELEASED

    def test_withdraw_by_the_employee(self, db_session: Any, authenticate: Any) -> None:
        f = Fixture(db_session, authenticate)
        application = f.apply(working_day(6)).json()
        response = f.client.post(
            f"{f.w.base}/me/leave-applications/{application['id']}/withdraw",
            json={"reason": "Plans changed"},
            headers=if_match(application),
        )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "WITHDRAWN"
        assert f.balance()["reserved_days"] == "0.00"

    def test_stale_etag_is_refused(self, db_session: Any, authenticate: Any) -> None:
        f = Fixture(db_session, authenticate)
        application = f.apply(working_day(6)).json()
        stale = f.manager_client.post(
            f"{f.w.base}/leave-applications/{application['id']}/approve",
            json={},
            headers={"If-Match": str(application["row_version"] + 5)},
        )
        assert stale.status_code == 412

    def test_only_the_assigned_approver_may_decide(
        self, db_session: Any, authenticate: Any
    ) -> None:
        f = Fixture(db_session, authenticate)
        application = f.apply(working_day(6)).json()
        other_manager = f.w.manager_client(f.w.employee(first_name="Omar"))
        viewer, _ = f.w.client_for(None, Role.READ_ONLY_VIEWER)
        for client in (other_manager, viewer):
            response = f.act(client, application, "approve")
            assert response.status_code == 403, response.text
        assert f.fetch(application["id"])["status"] == "SUBMITTED"

    def test_self_approval_is_refused_even_for_an_hr_admin(
        self, db_session: Any, authenticate: Any
    ) -> None:
        f = Fixture(db_session, authenticate)
        user = create_user(
            f.w.db,
            grants=[(f.w.org_id, Role.EMPLOYEE_SELF), (f.w.org_id, Role.HR_ADMIN)],
            employee_id=uuid.UUID(f.employee["id"]),
        )
        f.w.db.commit()
        both = f.w.authenticate(user)
        application = f.w.apply(both, f.leave_type, working_day(6)).json()
        response = f.act(both, application, "approve")
        assert response.status_code == 403
        assert _code(response) == ErrorCode.SELF_APPROVAL_NOT_ALLOWED.value

    def test_hr_override_needs_a_real_comment(self, db_session: Any, authenticate: Any) -> None:
        f = Fixture(db_session, authenticate)
        application = f.apply(working_day(6)).json()
        thin = f.act(f.w.hr, application, "approve", {"comment": "ok"})
        assert thin.status_code == 422
        done = f.act(
            f.w.hr, application, "approve", {"comment": "Manager is on leave; approving as HR."}
        )
        assert done.status_code == 200, done.text
        actions = [a["action"] for a in done.json()["actions"]]
        assert "OVERRIDE_APPROVED" in actions

    def test_manager_then_hr_needs_two_distinct_approvers(
        self, db_session: Any, authenticate: Any
    ) -> None:
        f = Fixture(db_session, authenticate, approval_chain="MANAGER_THEN_HR")
        application = f.apply(working_day(6)).json()
        level1 = f.act(f.manager_client, application, "approve")
        assert level1.status_code == 200, level1.text
        mid = level1.json()
        assert mid["status"] == "SUBMITTED" and mid["current_approver_type"] == "HR_QUEUE"
        assert f.balance()["reserved_days"] == "1.00"
        final = f.act(f.w.hr, mid, "approve")
        assert final.status_code == 200, final.text
        assert final.json()["status"] == "APPROVED"

    def test_no_manager_routes_to_the_hr_queue_and_hr_can_reassign(
        self, db_session: Any, authenticate: Any
    ) -> None:
        f = Fixture(db_session, authenticate)
        orphan = f.w.employee(first_name="Ola")
        client = f.w.self_client(orphan)
        f.w.opening(orphan, f.leave_type, "5.00")
        application = f.w.apply(client, f.leave_type, working_day(6)).json()
        assert application["current_approver_type"] == "HR_QUEUE"
        queue = f.w.hr.get(f"{f.w.base}/leave-applications?hr_queue=true").json()
        assert [a["id"] for a in queue["items"]] == [application["id"]]
        back = f.act(
            f.w.hr, application, "reassign", {"to_hr_queue": True, "reason": "Re-queue for audit"}
        )
        assert back.status_code == 200, back.text

    def test_employee_cannot_read_a_colleagues_application(
        self, db_session: Any, authenticate: Any
    ) -> None:
        f = Fixture(db_session, authenticate)
        application = f.apply(working_day(6)).json()
        stranger = f.w.self_client(f.w.employee(first_name="Sam"))
        response = stranger.get(f"{f.w.base}/leave-applications/{application['id']}")
        assert response.status_code == 404

    def test_self_service_flag_gates_the_me_endpoints(
        self, db_session: Any, authenticate: Any
    ) -> None:
        f = Fixture(db_session, authenticate)
        f.w._settings(enable_leave_self_service=False)
        response = f.apply(working_day(6))
        assert response.status_code == 403
        assert _code(response) == ErrorCode.SELF_SERVICE_NOT_ENABLED.value


class TestCancellation:
    def _approved_three_days(self, f: Fixture) -> dict[str, Any]:
        monday = next_weekday(1, after=7)
        application = f.apply(monday, monday + timedelta(days=2)).json()
        approved = f.act(f.manager_client, application, "approve")
        assert approved.status_code == 200, approved.text
        assert approved.json()["consumed_days"] == "3.00"
        return dict(approved.json())

    def test_tail_cancellation_restores_once_then_hr_cancels_the_rest(
        self, db_session: Any, authenticate: Any
    ) -> None:
        f = Fixture(db_session, authenticate)
        approved = self._approved_three_days(f)
        tuesday = next_weekday(2, after=7)
        body = {
            "scope": "FROM",
            "cancel_from_date": tuesday.isoformat(),
            "cancel_from_half": "FIRST_HALF",
            "reason": "Trip postponed to next month",
        }
        url = f"{f.w.base}/me/leave-applications/{approved['id']}/cancel"
        first = f.client.post(url, json=body, headers=if_match(approved))
        assert first.status_code == 200, first.text
        data = first.json()
        assert data["restored_days"] == "2.00" and data["consumed_days"] == "1.00"
        assert data["status"] == "APPROVED" and data["has_partial_cancellation"] is True
        assert f.balance()["posted_balance_days"] == "9.00"

        retry = f.client.post(url, json=body, headers=if_match(data))
        assert retry.status_code == 422
        assert _code(retry) == ErrorCode.LEAVE_NOT_CANCELLABLE.value
        assert f.balance()["posted_balance_days"] == "9.00"  # nothing restored twice

        rest = f.w.hr.post(
            f"{f.w.base}/leave-applications/{approved['id']}/cancel",
            json={"scope": "FULL", "reason": "Cancelled by HR after the project change"},
            headers=if_match(data),
        )
        assert rest.status_code == 200, rest.text
        assert rest.json()["status"] == "CANCELLED"
        assert f.balance()["posted_balance_days"] == "10.00"
        ledger = f.w.hr.get(f"{f.w.base}/employees/{f.employee['id']}/leave-transactions").json()
        assert [t["transaction_type"] for t in ledger["items"]].count("CONSUMPTION_REVERSAL") == 2

    def test_employee_cannot_cancel_started_leave(self, db_session: Any, authenticate: Any) -> None:
        f = Fixture(db_session, authenticate)
        day = working_day(0)
        application = f.apply(day).json()
        approved = f.act(f.manager_client, application, "approve").json()
        response = f.client.post(
            f"{f.w.base}/me/leave-applications/{approved['id']}/cancel",
            json={"scope": "FULL", "reason": "Changed my mind about today"},
            headers=if_match(approved),
        )
        assert response.status_code == 422
        assert _code(response) == ErrorCode.LEAVE_NOT_CANCELLABLE.value
