"""ATTENDANCE_DAY_CLOSE and ATTENDANCE_AUTO_FINALIZE (addendum 5.12, 14.1)."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

from app.core.permissions import Role
from app.jobs.runner import run_attendance_jobs
from tests.attendance_support import ist, make_attendance_world, past_working_day
from tests.job_architecture import if_match
from tests.leave_support import today


def _close(w: Any, day: Any, client: Any = None, key: str | None = None) -> Any:
    return (client or w.hr).post(
        f"{w.base}/attendance/day-close-runs",
        json={"business_date": day.isoformat()},
        headers={"Idempotency-Key": key or str(uuid.uuid4())},
    )


def test_day_close_creates_missing_records_and_is_idempotent(
    db_session: Any, authenticate: Any
) -> None:
    w = make_attendance_world(db_session, authenticate)
    worker = w.employee()
    day = past_working_day(10)
    w.day(worker, day, ((9, 0), (18, 0)))
    absentee = w.employee(first_name="Asha")

    first = _close(w, day)
    assert first.status_code == 201, first.text
    assert first.json()["status"] == "SUCCEEDED" and first.json()["failed_count"] == 0
    created = w.record(absentee, day)
    assert created.status_code == 200, created.text
    assert created.json()["day_status"] == "ABSENT" and created.json()["punches"] == []
    assert w.record(worker, day).json()["day_status"] == "PRESENT"

    before = w.record(absentee, day).json()
    again = _close(w, day)
    assert again.status_code == 201
    after = w.record(absentee, day).json()
    assert (after["id"], after["revision_count"], after["day_status"]) == (
        before["id"],
        before["revision_count"],
        before["day_status"],
    )


def test_day_close_respects_joining_dates_weekly_offs_and_leave(
    db_session: Any, authenticate: Any
) -> None:
    w = make_attendance_world(db_session, authenticate)
    leave_type = w.leave_type("CL")
    w.policy(leave_type, max_backdate_days=365)
    manager = w.employee(first_name="Mira")
    employee = w.employee(first_name="Esha")
    w.set_manager(employee["id"], manager["id"])
    w.opening(employee, leave_type, "5.00")
    late_joiner = w.employee(first_name="Late", joining_date=past_working_day(8).isoformat())

    day = past_working_day(12)
    manager_client = w.manager_client(
        manager
    )  # must exist before submission, or routing falls back to HR
    application = w.apply(w.self_client(employee), leave_type, day).json()
    approved = manager_client.post(
        f"{w.base}/leave-applications/{application['id']}/approve",
        json={},
        headers=if_match(application),
    )
    assert approved.status_code == 200, approved.text

    saturday = day
    while saturday.isoweekday() != 6:
        saturday += timedelta(days=1)
    for target in (day, saturday):
        assert _close(w, target).status_code == 201

    on_leave = w.record(employee, day).json()
    assert on_leave["day_status"] == "ON_LEAVE"
    assert on_leave["first_half_leave_application_id"] == application["id"]
    assert w.record(employee, saturday).json()["day_status"] == "WEEKLY_OFF"
    assert w.record(late_joiner, day).status_code == 404  # not yet joined: no record (ATT-01)


def test_manual_day_close_is_bounded_authorized_and_replays_by_key(
    db_session: Any, authenticate: Any
) -> None:
    w = make_attendance_world(db_session, authenticate)
    day = past_working_day(10)
    assert _close(w, today()).status_code == 422
    assert _close(w, today() - timedelta(days=40)).status_code == 422
    hre, _ = w.client_for(None, Role.HR_EXECUTIVE)
    assert _close(w, day, client=hre).status_code == 403

    key = str(uuid.uuid4())
    first = _close(w, day, key=key)
    second = _close(w, day, key=key)
    assert first.json()["id"] == second.json()["id"]


def test_auto_finalize_closes_old_open_records_and_marks_incomplete_absent(
    db_session: Any, authenticate: Any
) -> None:
    w = make_attendance_world(
        db_session, authenticate, auto_finalize_after_days=1, incomplete_on_finalize="MARK_ABSENT"
    )
    complete = w.employee()
    incomplete = w.employee(first_name="Inge")
    day = past_working_day(10)
    w.day(complete, day, ((9, 0), (18, 0)))
    assert w.punch(incomplete, ist(day, 9, 0), "IN").status_code == 200  # no OUT

    report = run_attendance_jobs(db_session, w.org_id)
    assert report["attendance_auto_finalized"] >= 2, report

    done = w.record(complete, day).json()
    assert done["record_state"] == "FINALIZED" and done["day_status"] == "PRESENT"
    forced = w.record(incomplete, day).json()
    assert forced["record_state"] == "FINALIZED"
    assert forced["day_status"] == "ABSENT"  # MARK_ABSENT resolved the missing OUT
    revisions = w.hr.get(
        f"{w.base}/employees/{incomplete['id']}/attendance/{day}/revisions"
    ).json()["items"]
    assert revisions[-1]["change_source"] == "FINALIZED"

    # A rerun finds nothing left to finalize and changes nothing.
    again = run_attendance_jobs(db_session, w.org_id)
    assert again["attendance_auto_finalized"] == 0
    assert w.record(complete, day).json()["revision_count"] == done["revision_count"]
