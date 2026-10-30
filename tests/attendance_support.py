"""Shared setup for the attendance suites (addendum 5): a world with leave and attendance on."""

from __future__ import annotations

import uuid
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.core.permissions import Role
from tests.factories import enable_configurable_permission
from tests.job_architecture import if_match
from tests.leave_support import LeaveWorld, make_world, today, working_day

IST = ZoneInfo("Asia/Kolkata")


def past_working_day(days_ago: int = 12) -> date:
    """A Monday-Friday safely in the past, so the attendance window has closed."""
    day = today() - timedelta(days=days_ago)
    while day.isoweekday() > 5:
        day -= timedelta(days=1)
    return day


def ist(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=IST)


class AttendanceWorld(LeaveWorld):
    shift: dict[str, Any]
    attendance_policy: dict[str, Any]
    pattern_id: str

    def enable_attendance(self, **policy_fields: Any) -> None:
        self._settings(
            enable_attendance_module=True,
            enable_attendance_self_service=True,
            attendance_tracking_start_date=date(2026, 1, 1),
        )
        enable_configurable_permission(
            self.db,
            organization_id=self.org_id,
            role=Role.EMPLOYEE_SELF,
            permission_code="self.attendance",
        )
        self.db.commit()
        patterns = self.hr.get(f"{self.base}/weekly-off-patterns").json()["items"]
        self.pattern_id = next(p["id"] for p in patterns if p["pattern_code"] == "SATSUN")
        self.shift = self.make_shift("DAY")
        self.attendance_policy = self.make_policy(
            "STD", default_shift_id=self.shift["id"], **policy_fields
        )

    def make_shift(self, code: str, **overrides: Any) -> dict[str, Any]:
        body = {
            "shift_code": code,
            "shift_name": f"{code} shift",
            "start_time": "09:00:00",
            "end_time": "18:00:00",
            "break_minutes": 60,
            "full_day_min_work_minutes": 480,
            "half_day_min_work_minutes": 240,
        }
        body.update(overrides)
        response = self.hr.post(f"{self.base}/shifts", json=body)
        assert response.status_code == 201, response.text
        return dict(response.json())

    def make_policy(self, code: str, *, publish: bool = True, **fields: Any) -> dict[str, Any]:
        version = {
            "effective_from": "2026-01-01",
            "default_weekly_off_pattern_id": self.pattern_id,
            "break_deduction_mode": "FIXED_FROM_SHIFT",
            "self_punch_enabled": True,
            "integration_punch_enabled": True,
            "late_grace_minutes": 10,
            "early_departure_grace_minutes": 10,
        }
        version.update(fields)
        response = self.hr.post(
            f"{self.base}/attendance-policies",
            json={"policy_code": code, "policy_name": f"{code} policy", "version": version},
        )
        assert response.status_code == 201, response.text
        policy = dict(response.json())
        if publish:
            draft = policy["versions"][0]
            published = self.hr.post(
                f"{self.base}/attendance-policies/{policy['id']}/versions/{draft['id']}/publish",
                headers=if_match(draft),
            )
            assert published.status_code == 200, published.text
        return policy

    # Punches and records.

    def punch(
        self, employee: dict[str, Any], at: datetime, kind: str, client: Any = None, **extra: Any
    ) -> Any:
        body = {
            "punch_at": at.isoformat(),
            "punch_type": kind,
            "remarks": "Entered by HR from the register",
        }
        body.update(extra)
        return (client or self.hr).post(
            f"{self.base}/employees/{employee['id']}/attendance/punches", json=body
        )

    def day(
        self, employee: dict[str, Any], day: date, hours: tuple[tuple[int, int], tuple[int, int]]
    ) -> None:
        (h1, m1), (h2, m2) = hours
        assert self.punch(employee, ist(day, h1, m1), "IN").status_code == 200
        assert self.punch(employee, ist(day, h2, m2), "OUT").status_code == 200

    def record(self, employee: dict[str, Any], day: date, client: Any = None) -> Any:
        return (client or self.hr).get(f"{self.base}/employees/{employee['id']}/attendance/{day}")

    def finalize(self, employee: dict[str, Any], start: date, end: date | None = None) -> Any:
        return self.hr.post(
            f"{self.base}/attendance/finalize",
            json={
                "employee_id": employee["id"],
                "from": start.isoformat(),
                "to": (end or start).isoformat(),
            },
        )

    def correct(
        self,
        employee: dict[str, Any],
        day: date,
        operations: list[dict[str, Any]],
        client: Any = None,
    ) -> Any:
        record = self.record(employee, day).json()
        return (client or self.hr).post(
            f"{self.base}/employees/{employee['id']}/attendance/{day}/corrections",
            json={"operations": operations, "reason": "Correction after reviewing the register"},
            headers=if_match(record),
        )

    def add_source(self, code: str = "ACCESS_CTRL_1") -> None:
        response = self.hr.post(
            f"{self.base}/lookups/ATTENDANCE_SOURCE",
            json={"list_code": "ATTENDANCE_SOURCE", "value_code": code, "label": "Door controller"},
        )
        assert response.status_code == 201, response.text


def make_attendance_world(
    db_session: Any, authenticate: Any, **policy_fields: Any
) -> AttendanceWorld:
    base = make_world(db_session, authenticate)
    world = AttendanceWorld(base.arch, db_session, authenticate)
    world.enable_attendance(**policy_fields)
    return world


def new_uuid() -> str:
    return str(uuid.uuid4())


__all__ = [
    "AttendanceWorld",
    "ist",
    "make_attendance_world",
    "new_uuid",
    "past_working_day",
    "working_day",
]
