"""Leave and attendance integration: AC-43, INT-10..INT-13, LEV-30, LEV-31 (addendum 7.4)."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from app.core.errors import ErrorCode
from tests.attendance_support import AttendanceWorld, ist, make_attendance_world, past_working_day
from tests.job_architecture import if_match
from tests.leave_support import today

PUNCH_CONFLICT = "W-LEAVE-PUNCH-CONFLICT"


def _code(response: Any) -> str:
    return str(response.json()["error"]["code"])


def _monday_pair() -> tuple[date, date]:
    day = today() - timedelta(days=21)
    while day.isoweekday() != 1:
        day -= timedelta(days=1)
    return day, day + timedelta(days=1)


class Fixture:
    def __init__(self, db: Any, authenticate: Any) -> None:
        self.w: AttendanceWorld = make_attendance_world(db, authenticate)
        self.leave_type = self.w.leave_type("CL")
        self.w.policy(self.leave_type, max_backdate_days=365)
        self.manager = self.w.employee(first_name="Mira")
        self.employee = self.w.employee(first_name="Esha")
        self.w.set_manager(self.employee["id"], self.manager["id"])
        self.manager_client = self.w.manager_client(self.manager)
        self.client = self.w.self_client(self.employee)
        self.w.opening(self.employee, self.leave_type, "10.00")

    def apply(self, start: date, end: date | None = None, **extra: Any) -> Any:
        return self.w.apply(self.client, self.leave_type, start, end, **extra)

    def approve(self, application: dict[str, Any], **body: Any) -> Any:
        return self.manager_client.post(
            f"{self.w.base}/leave-applications/{application['id']}/approve",
            json=body,
            headers=if_match(application),
        )

    def revisions(self, day: date) -> list[dict[str, Any]]:
        url = f"{self.w.base}/employees/{self.employee['id']}/attendance/{day}/revisions"
        return list(self.w.hr.get(url).json()["items"])


def _absent_finalized_day(f: Fixture, day: date) -> None:
    """A FINALIZED ABSENT record: punch, then void it by correction, then finalize."""
    assert f.w.punch(f.employee, ist(day, 9, 0), "IN").status_code == 200
    punch_id = f.w.record(f.employee, day).json()["punches"][0]["id"]
    assert f.w.correct(f.employee, day, [{"void_punch_id": punch_id}]).status_code == 200
    done = f.w.finalize(f.employee, day)
    assert done.json()["finalized_count"] == 1, done.text
    assert f.w.record(f.employee, day).json()["day_status"] == "ABSENT"


def test_ac43_leave_over_finalized_absent_and_open_worked_day(
    db_session: Any, authenticate: Any
) -> None:
    f = Fixture(db_session, authenticate)
    d0, d1 = _monday_pair()
    _absent_finalized_day(f, d0)
    f.w.day(f.employee, d1, ((9, 0), (18, 0)))

    refused = f.apply(d0, d1)
    assert refused.status_code == 422
    assert _code(refused) == ErrorCode.WARNINGS_REQUIRE_ACKNOWLEDGEMENT.value
    assert PUNCH_CONFLICT in str(refused.json()["error"]["warnings"])
    submitted = f.apply(d0, d1, acknowledged_warnings=[PUNCH_CONFLICT])
    assert submitted.status_code == 201, submitted.text
    application = submitted.json()
    assert application["requested_days"] == "2.00"

    unacknowledged = f.approve(application)
    assert unacknowledged.status_code == 422  # re-raised at final approval
    approved = f.approve(application, acknowledged_warnings=[PUNCH_CONFLICT])
    assert approved.status_code == 200, approved.text

    for day in (d0, d1):
        record = f.w.record(f.employee, day).json()
        assert (record["first_half_status"], record["second_half_status"]) == (
            "ON_LEAVE",
            "ON_LEAVE",
        )
        assert record["first_half_leave_application_id"] == application["id"]
        assert record["second_half_leave_application_id"] == application["id"]
        assert record["day_status"] == "ON_LEAVE"
    finalized, open_record = (f.w.record(f.employee, d).json() for d in (d0, d1))
    assert (
        finalized["record_state"] == "FINALIZED" and finalized["post_finalization_change"] is True
    )
    assert open_record["record_state"] == "OPEN" and open_record["punch_on_leave_conflict"] is True
    assert len(open_record["punches"]) == 2  # punches are retained

    leave_revision = next(r for r in f.revisions(d0) if r["change_source"] == "LEAVE_APPROVED")
    assert leave_revision["before_snapshot"]["first_half_status"] == "ABSENT"
    assert leave_revision["after_snapshot"]["first_half_status"] == "ON_LEAVE"
    open_revision = next(r for r in f.revisions(d1) if r["change_source"] == "LEAVE_APPROVED")
    assert open_revision["before_snapshot"]["day_status"] == "PRESENT"


def test_cancellation_removes_the_leave_halves_and_keeps_finalized_records_complete(
    db_session: Any, authenticate: Any
) -> None:
    f = Fixture(db_session, authenticate)
    d0, d1 = _monday_pair()
    _absent_finalized_day(f, d0)
    f.w.day(f.employee, d1, ((9, 0), (18, 0)))
    application = f.apply(d0, d1, acknowledged_warnings=[PUNCH_CONFLICT]).json()
    approved = f.approve(application, acknowledged_warnings=[PUNCH_CONFLICT]).json()

    cancelled = f.w.hr.post(
        f"{f.w.base}/leave-applications/{approved['id']}/cancel",
        json={"scope": "FULL", "reason": "Cancelled by HR: the employee worked these days"},
        headers=if_match(approved),
    )
    assert cancelled.status_code == 200, cancelled.text

    finalized = f.w.record(f.employee, d0).json()
    assert finalized["record_state"] == "FINALIZED"
    assert (finalized["first_half_status"], finalized["second_half_status"]) == ("ABSENT", "ABSENT")
    assert finalized["first_half_leave_application_id"] is None
    reopened_day = f.w.record(f.employee, d1).json()
    assert (
        reopened_day["day_status"] == "PRESENT" and reopened_day["punch_on_leave_conflict"] is False
    )
    assert "LEAVE_CANCELLED" in [r["change_source"] for r in f.revisions(d0)]
    assert "LEAVE_CANCELLED" in [r["change_source"] for r in f.revisions(d1)]
    assert f.w.balance(f.employee, f.leave_type)["posted_balance_days"] == "10.00"


def test_records_created_after_approval_start_on_leave(db_session: Any, authenticate: Any) -> None:
    """INT-11: a punch on an approved-leave day creates the record with LEAVE halves."""
    f = Fixture(db_session, authenticate)
    day = past_working_day(30)
    application = f.apply(day).json()
    assert f.approve(application).status_code == 200
    assert f.w.punch(f.employee, ist(day, 9, 0), "IN").status_code == 200
    record = f.w.record(f.employee, day).json()
    assert record["first_half_status"] == "ON_LEAVE" and record["second_half_status"] == "ON_LEAVE"
    assert record["punch_on_leave_conflict"] is True
    balance = f.w.balance(f.employee, f.leave_type)
    assert (
        balance["posted_balance_days"] == "9.00"
    )  # punching on leave never changes the balance (INT-13)


def test_half_day_leave_marks_one_half_and_late_is_measured_from_the_midpoint(
    db_session: Any, authenticate: Any
) -> None:
    f = Fixture(db_session, authenticate)
    day = past_working_day(25)
    application = f.apply(day, day, end_half="FIRST_HALF").json()
    assert f.approve(application).status_code == 200
    assert (
        f.w.punch(f.employee, ist(day, 13, 40), "IN").status_code == 200
    )  # shift midpoint is 13:30
    assert f.w.punch(f.employee, ist(day, 18, 50), "OUT").status_code == 200
    record = f.w.record(f.employee, day).json()
    assert record["first_half_status"] == "ON_LEAVE" and record["second_half_status"] == "PRESENT"
    assert record["day_status"] == "SPLIT"
    assert (
        record["late_by_minutes"] == 10 and record["is_late"] is False
    )  # within the 10-minute grace


def test_leave_cannot_land_on_an_overridden_half_and_overrides_cannot_hit_leave(
    db_session: Any, authenticate: Any
) -> None:
    f = Fixture(db_session, authenticate)
    day = past_working_day(40)
    f.w.day(f.employee, day, ((9, 0), (18, 0)))
    override = f.w.correct(
        f.employee, day, [{"set_override": {"half": "FIRST_HALF", "status": "PRESENT"}}]
    )
    assert override.status_code == 200, override.text
    conflict = f.apply(day, day, acknowledged_warnings=[PUNCH_CONFLICT])
    assert conflict.status_code == 409
    assert _code(conflict) == ErrorCode.LEAVE_CONFLICTS_WITH_ATTENDANCE_OVERRIDE.value

    free_day = past_working_day(50)
    application = f.apply(free_day).json()
    assert f.approve(application).status_code == 200
    f.w.punch(f.employee, ist(free_day, 9, 0), "IN")  # creates the record with LEAVE halves
    blocked = f.w.correct(
        f.employee, free_day, [{"set_override": {"half": "FIRST_HALF", "status": "ABSENT"}}]
    )
    assert blocked.status_code == 422
    assert _code(blocked) == ErrorCode.ATTENDANCE_CONFLICTS_WITH_LEAVE.value


def test_pending_leave_never_touches_attendance(db_session: Any, authenticate: Any) -> None:
    f = Fixture(db_session, authenticate)
    day = past_working_day(35)
    f.w.day(f.employee, day, ((9, 0), (18, 0)))
    assert f.apply(day, acknowledged_warnings=[PUNCH_CONFLICT]).status_code == 201
    record = f.w.record(f.employee, day).json()
    assert record["day_status"] == "PRESENT" and record["punch_on_leave_conflict"] is False
