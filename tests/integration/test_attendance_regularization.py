"""Attendance regularization (addendum 5.14, ATT-20..23, AC-40, AC-41)."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

from app.core.errors import ErrorCode
from app.core.permissions import Role
from tests.attendance_support import AttendanceWorld, ist, make_attendance_world, past_working_day
from tests.factories import create_user
from tests.job_architecture import if_match


def _code(response: Any) -> str:
    return str(response.json()["error"]["code"])


class Fixture:
    def __init__(self, db: Any, authenticate: Any, **policy: Any) -> None:
        self.w: AttendanceWorld = make_attendance_world(db, authenticate, **policy)
        self.manager = self.w.employee(first_name="Mira")
        self.employee = self.w.employee(first_name="Esha")
        self.w.set_manager(self.employee["id"], self.manager["id"])
        self.manager_client = self.w.manager_client(self.manager)
        self.client = self.w.self_client(self.employee)
        # The manager must also hold the approve permission: DM does by default.

    def missing_out(self, day: date) -> None:
        assert self.w.punch(self.employee, ist(day, 9, 0), "IN").status_code == 200

    def submit(self, day: date, items: list[dict[str, Any]] | None = None, **extra: Any) -> Any:
        body = {
            "business_date": day.isoformat(),
            "request_type": "MISSING_PUNCH",
            "reason": "Forgot to punch out at the end of the day",
            "items": items
            or [
                {
                    "item_type": "ADD_PUNCH",
                    "punch_type": "OUT",
                    "punch_at": ist(day, 18, 0).isoformat(),
                }
            ],
        }
        body.update(extra)
        return self.client.post(f"{self.w.base}/me/attendance/regularizations", json=body)

    def act(self, client: Any, request: dict[str, Any], verb: str, body: Any = None) -> Any:
        return client.post(
            f"{self.w.base}/attendance/regularizations/{request['id']}/{verb}",
            json=body or {},
            headers=if_match(request),
        )


def test_ac40_missing_out_is_regularized_and_the_revision_keeps_before_and_after(
    db_session: Any, authenticate: Any
) -> None:
    f = Fixture(db_session, authenticate)
    day = past_working_day(3)
    f.missing_out(day)
    assert f.w.record(f.employee, day).json()["day_status"] == "INCOMPLETE"

    submitted = f.submit(day)
    assert submitted.status_code == 201, submitted.text
    request = submitted.json()
    assert request["status"] == "SUBMITTED" and request["current_approver_type"] == "MANAGER"
    assert f.w.record(f.employee, day).json()["has_pending_regularization"] is True

    approved = f.act(f.manager_client, request, "approve")
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "APPROVED"

    record = f.w.record(f.employee, day).json()
    assert record["day_status"] == "PRESENT" and record["has_pending_regularization"] is False
    sources = sorted(p["source"] for p in record["punches"])
    assert sources == ["MANUAL", "REGULARIZATION"]
    revision = next(
        r
        for r in f.w.hr.get(
            f"{f.w.base}/employees/{f.employee['id']}/attendance/{day}/revisions"
        ).json()["items"]
        if r["change_source"] == "REGULARIZATION_APPROVED"
    )
    assert revision["before_snapshot"]["day_status"] == "INCOMPLETE"
    assert revision["after_snapshot"]["day_status"] == "PRESENT"
    assert [a["action"] for a in approved.json()["actions"]] == ["SUBMITTED", "APPROVED_FINAL"]


def test_ac41_self_approval_is_refused_and_rejection_leaves_the_record_unchanged(
    db_session: Any, authenticate: Any
) -> None:
    f = Fixture(db_session, authenticate)
    day = past_working_day(3)
    f.missing_out(day)
    user = create_user(
        f.w.db,
        grants=[(f.w.org_id, Role.EMPLOYEE_SELF), (f.w.org_id, Role.HR_ADMIN)],
        employee_id=uuid.UUID(f.employee["id"]),
    )
    f.w.db.commit()  # commit before logging in: login reads through another session
    both = f.w.authenticate(user)
    request = both.post(
        f"{f.w.base}/me/attendance/regularizations",
        json={
            "business_date": day.isoformat(),
            "request_type": "MISSING_PUNCH",
            "reason": "Forgot to punch out at the end of the day",
            "items": [
                {
                    "item_type": "ADD_PUNCH",
                    "punch_type": "OUT",
                    "punch_at": ist(day, 18, 0).isoformat(),
                }
            ],
        },
    ).json()
    own = f.act(both, request, "approve")
    assert own.status_code == 403 and _code(own) == ErrorCode.SELF_APPROVAL_NOT_ALLOWED.value

    before = f.w.record(f.employee, day).json()
    rejected = f.act(
        f.manager_client, request, "reject", {"decision_comment": "Not supported by the door log"}
    )
    assert rejected.status_code == 200 and rejected.json()["status"] == "REJECTED"
    after = f.w.record(f.employee, day).json()
    assert after["has_pending_regularization"] is False
    assert (after["day_status"], len(after["punches"]), after["revision_count"]) == (
        before["day_status"],
        len(before["punches"]),
        before["revision_count"],
    )
    again = f.act(f.manager_client, rejected.json(), "approve")
    assert _code(again) == ErrorCode.INVALID_APPROVAL_TRANSITION.value


def test_submission_rules(db_session: Any, authenticate: Any) -> None:
    f = Fixture(db_session, authenticate)
    day = past_working_day(3)
    f.missing_out(day)
    first = f.submit(day)
    assert first.status_code == 201
    second = f.submit(day)
    assert (
        second.status_code == 409
        and _code(second) == ErrorCode.REGULARIZATION_ALREADY_PENDING.value
    )

    old = past_working_day(30)
    f.w.punch(f.employee, ist(old, 9, 0), "IN")
    expired = f.submit(old)
    assert expired.status_code == 422
    assert _code(expired) == ErrorCode.REGULARIZATION_WINDOW_EXPIRED.value

    invalid = f.submit(
        past_working_day(4), items=[{"item_type": "SET_ON_DUTY", "half": "FIRST_HALF"}]
    )
    assert invalid.status_code == 422  # a MISSING_PUNCH request cannot carry on-duty items

    other = past_working_day(5)
    f.w.day(f.employee, other, ((9, 0), (18, 0)))
    duplicate = f.submit(
        other,
        items=[
            {
                "item_type": "ADD_PUNCH",
                "punch_type": "OUT",
                "punch_at": ist(other, 18, 0).isoformat(),
            }
        ],
    )
    assert _code(duplicate) == ErrorCode.REGULARIZATION_ITEMS_INVALID.value


def test_finalized_record_cannot_be_regularized_and_approval_fails_if_it_finalizes_meanwhile(
    db_session: Any, authenticate: Any
) -> None:
    f = Fixture(db_session, authenticate, incomplete_on_finalize="MARK_ABSENT")
    day = past_working_day(3)
    f.missing_out(day)
    request = f.submit(day).json()
    # Finalization skips a record with a pending request (ATT-22 precondition).
    skipped = f.w.finalize(f.employee, day)
    assert skipped.json()["skipped"][0]["reason"] == "PENDING_REGULARIZATION"

    f.act(
        f.manager_client, request, "reject", {"decision_comment": "Withdrawn by mistake, redo it"}
    )
    assert f.w.finalize(f.employee, day).json()["finalized_count"] == 1
    late = f.submit(day)
    assert late.status_code == 422 and _code(late) == ErrorCode.ATTENDANCE_RECORD_FINALIZED.value


def test_on_duty_request_needs_the_policy_flag_and_marks_the_half(
    db_session: Any, authenticate: Any
) -> None:
    f = Fixture(db_session, authenticate)
    day = past_working_day(3)
    f.missing_out(day)
    refused = f.submit(
        day, request_type="ON_DUTY", items=[{"item_type": "SET_ON_DUTY", "half": "FIRST_HALF"}]
    )
    assert refused.status_code == 422

    g = Fixture(db_session, authenticate, on_duty_requests_enabled=True)
    g.missing_out(day)
    request = g.submit(
        day, request_type="ON_DUTY", items=[{"item_type": "SET_ON_DUTY", "half": "FIRST_HALF"}]
    ).json()
    assert g.act(g.manager_client, request, "approve").status_code == 200
    record = g.w.record(g.employee, day).json()
    assert record["first_half_status"] == "PRESENT" and record["first_half_source"] == "ON_DUTY"
    assert record["work_arrangement"] == "ON_DUTY"


def test_manager_then_hr_chain_and_withdraw_and_isolation(
    db_session: Any, authenticate: Any
) -> None:
    f = Fixture(db_session, authenticate, regularization_approval_chain="MANAGER_THEN_HR")
    day = past_working_day(3)
    f.missing_out(day)
    request = f.submit(day).json()
    level1 = f.act(f.manager_client, request, "approve")
    assert (
        level1.json()["status"] == "SUBMITTED"
        and level1.json()["current_approver_type"] == "HR_QUEUE"
    )
    again = f.act(f.manager_client, level1.json(), "approve")
    assert again.status_code == 403  # an HR-queue level needs the HR permission
    final = f.act(f.w.hr, level1.json(), "approve")
    assert final.status_code == 200 and final.json()["status"] == "APPROVED"

    other_day = past_working_day(4)
    f.missing_out(other_day)
    pending = f.submit(other_day).json()
    withdrawn = f.client.post(
        f"{f.w.base}/me/attendance/regularizations/{pending['id']}/withdraw",
        json={},
        headers=if_match(pending),
    )
    assert withdrawn.status_code == 200 and withdrawn.json()["status"] == "WITHDRAWN"
    assert f.w.record(f.employee, other_day).json()["has_pending_regularization"] is False

    stranger = make_attendance_world(db_session, authenticate)
    assert (
        stranger.hr.get(f"{f.w.base}/attendance/regularizations/{pending['id']}").status_code == 403
    )
    assert (
        stranger.hr.get(f"{stranger.base}/attendance/regularizations/{pending['id']}").status_code
        == 404
    )
