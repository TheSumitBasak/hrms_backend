"""Employee lifecycle effects on leave and attendance (addendum 7.5, INT-36, AC-50, AC-51)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from tests.attendance_support import AttendanceWorld, ist, make_attendance_world, past_working_day
from tests.job_architecture import if_match
from tests.leave_support import next_weekday, today


class Fixture:
    def __init__(self, db: Any, authenticate: Any) -> None:
        self.w: AttendanceWorld = make_attendance_world(db, authenticate)
        self.leave_type = self.w.leave_type("CL")
        self.w.policy(self.leave_type, max_backdate_days=365)
        self.m1 = self.w.employee(first_name="Mira")
        self.m2 = self.w.employee(first_name="Omar")
        self.employee = self.w.employee(first_name="Esha")
        self.w.set_manager(self.employee["id"], self.m1["id"])
        # Manager accounts must exist before submission, or routing falls back to the HR queue.
        self.m1_client = self.w.manager_client(self.m1)
        self.m2_client = self.w.manager_client(self.m2)
        self.client = self.w.self_client(self.employee)
        self.w.opening(self.employee, self.leave_type, "10.00")

    def job_change(self, **fields: Any) -> Any:
        return self.w.hr.post(
            f"{self.w.base}/employees/{self.employee['id']}/job-changes",
            json={
                "effective_date": today().isoformat(),
                "change_type": "TRANSFER",
                "reason": "Reporting line changed",
                **fields,
            },
        )

    def leave(self, application_id: str) -> dict[str, Any]:
        return dict(self.w.hr.get(f"{self.w.base}/leave-applications/{application_id}").json())


def test_ac50_manager_change_reroutes_pending_requests(db_session: Any, authenticate: Any) -> None:
    f = Fixture(db_session, authenticate)
    leave = f.w.apply(f.client, f.leave_type, next_weekday(1, after=7)).json()
    assert leave["current_approver_type"] == "MANAGER"

    day = past_working_day(3)
    f.w.punch(f.employee, ist(day, 9, 0), "IN")
    regularization = f.client.post(
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

    changed = f.job_change(reporting_manager_employee_id=f.m2["id"])
    assert changed.status_code == 200, changed.text

    moved = f.leave(leave["id"])
    assert moved["current_approver_user_id"] != leave["current_approver_user_id"]
    assert [a["action"] for a in moved["actions"]][-1] == "REASSIGNED"
    assert moved["actions"][-1]["comment"] == "MANAGER_CHANGE"
    old = f.m1_client.post(
        f"{f.w.base}/leave-applications/{leave['id']}/approve", json={}, headers=if_match(moved)
    )
    assert old.status_code == 403
    new = f.m2_client.post(
        f"{f.w.base}/leave-applications/{leave['id']}/approve", json={}, headers=if_match(moved)
    )
    assert new.status_code == 200, new.text

    reg = f.w.hr.get(f"{f.w.base}/attendance/regularizations/{regularization['id']}").json()
    assert (
        reg["actions"][-1]["action"] == "REASSIGNED"
        and reg["actions"][-1]["comment"] == "MANAGER_CHANGE"
    )
    assert reg["current_approver_user_id"] != regularization["current_approver_user_id"]
    approved = f.m2_client.post(
        f"{f.w.base}/attendance/regularizations/{regularization['id']}/approve",
        json={},
        headers=if_match(reg),
    )
    assert approved.status_code == 200, approved.text


def test_ac51_employment_end_cancels_what_lies_beyond_it(
    db_session: Any, authenticate: Any
) -> None:
    f = Fixture(db_session, authenticate)
    wednesday = next_weekday(3, after=10)
    monday = wednesday - timedelta(days=2)
    friday = wednesday + timedelta(days=2)
    next_monday = wednesday + timedelta(days=5)

    approved = f.w.apply(f.client, f.leave_type, monday, friday).json()
    done = f.m1_client.post(
        f"{f.w.base}/leave-applications/{approved['id']}/approve",
        json={},
        headers=if_match(approved),
    )
    assert done.status_code == 200 and done.json()["consumed_days"] == "5.00", done.text
    pending = f.w.apply(f.client, f.leave_type, next_monday).json()

    past = past_working_day(3)
    f.w.punch(f.employee, ist(past, 9, 0), "IN")
    kept = f.client.post(
        f"{f.w.base}/me/attendance/regularizations",
        json={
            "business_date": past.isoformat(),
            "request_type": "MISSING_PUNCH",
            "reason": "Forgot to punch out at the end of the day",
            "items": [
                {
                    "item_type": "ADD_PUNCH",
                    "punch_type": "OUT",
                    "punch_at": ist(past, 18, 0).isoformat(),
                }
            ],
        },
    ).json()
    assert f.w.balance(f.employee, f.leave_type)["reserved_days"] == "1.00"

    noticed = f.w.hr.post(
        f"{f.w.base}/employees/{f.employee['id']}/status-transitions",
        json={
            "to_status": "ON_NOTICE",
            "status_effective_date": today().isoformat(),
            "employment_end_date": wednesday.isoformat(),
            "status_reason": "RESIGNATION",
        },
    )
    assert noticed.status_code == 200, noticed.text

    cancelled = f.leave(pending["id"])
    assert cancelled["status"] == "CANCELLED"
    assert cancelled["actions"][-1]["action"] == "SYSTEM_CANCELLED"
    assert cancelled["actions"][-1]["comment"] == "EMPLOYMENT_END"

    trimmed = f.leave(approved["id"])
    assert trimmed["status"] == "APPROVED" and trimmed["has_partial_cancellation"] is True
    assert trimmed["consumed_days"] == "3.00" and trimmed["cancelled_days"] == "2.00"
    balance = f.w.balance(f.employee, f.leave_type)
    assert (balance["posted_balance_days"], balance["reserved_days"]) == ("7.00", "0.00")

    still = f.w.hr.get(f"{f.w.base}/attendance/regularizations/{kept['id']}").json()
    assert still["status"] == "SUBMITTED"  # a date on or before the end date stays actionable


def test_archiving_cancels_pending_leave_only(db_session: Any, authenticate: Any) -> None:
    f = Fixture(db_session, authenticate)
    approved = f.w.apply(f.client, f.leave_type, next_weekday(1, after=14)).json()
    assert (
        f.m1_client.post(
            f"{f.w.base}/leave-applications/{approved['id']}/approve",
            json={},
            headers=if_match(approved),
        ).status_code
        == 200
    )
    pending = f.w.apply(f.client, f.leave_type, next_weekday(2, after=14)).json()

    archived = f.w.hr.post(
        f"{f.w.base}/employees/{f.employee['id']}/archive",
        json={"reason": "Duplicate record created in error, archived by HR"},
    )
    assert archived.status_code in (200, 204), archived.text
    assert f.leave(pending["id"])["status"] == "CANCELLED"
    assert f.leave(pending["id"])["actions"][-1]["comment"] == "EMPLOYEE_ARCHIVED"
    assert f.leave(approved["id"])["status"] == "APPROVED"  # history retained
