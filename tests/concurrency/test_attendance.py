"""Attendance concurrency: duplicate punches and replayed imports under parallel requests."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any

from app.core.permissions import Role
from tests.attendance_support import ist, make_attendance_world, past_working_day


def test_simultaneous_identical_punches_yield_one_valid(db_session: Any, authenticate: Any) -> None:
    """ATT-13 is check-then-insert; the employee row lock must serialise it."""
    w = make_attendance_world(db_session, authenticate)
    employee = w.employee()
    day = past_working_day()

    def punch(_: int) -> str:
        response = w.punch(employee, ist(day, 9, 0), "IN")
        assert response.status_code == 200, response.text
        return str(response.json()["outcome"])

    with ThreadPoolExecutor(max_workers=5) as pool:
        outcomes = list(pool.map(punch, range(5)))

    assert outcomes.count("CREATED") == 1, outcomes
    assert outcomes.count("DUPLICATE") == 4, outcomes
    punches = w.record(employee, day).json()["punches"]
    assert sorted(p["punch_status"] for p in punches) == ["DUPLICATE"] * 4 + ["VALID"]


def test_the_same_integration_event_arriving_in_parallel_is_stored_once(
    db_session: Any, authenticate: Any
) -> None:
    w = make_attendance_world(db_session, authenticate)
    w.add_source()
    employee = w.employee()
    client, _ = w.client_for(None, Role.ATTENDANCE_INTEGRATION_CLIENT)
    day = past_working_day()
    body = {
        "items": [
            {
                "employee_id": employee["id"],
                "punch_at": ist(day, 9, 0).isoformat(),
                "punch_type": "IN",
                "integration_source_code": "ACCESS_CTRL_1",
                "external_event_id": "race-1",
            }
        ]
    }

    def send(_: int) -> str:
        response = client.post(f"{w.base}/attendance/punch-imports", json=body)
        assert response.status_code == 200, response.text
        return str(response.json()["results"][0]["outcome"])

    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(send, range(4)))

    assert outcomes.count("CREATED") == 1 and outcomes.count("REPLAYED") == 3, outcomes
    assert len(w.record(employee, day).json()["punches"]) == 1


def test_parallel_manual_punches_for_one_employee_keep_the_record_consistent(
    db_session: Any, authenticate: Any
) -> None:
    w = make_attendance_world(db_session, authenticate)
    employee = w.employee()
    day = past_working_day()

    def punch(args: tuple[int, str]) -> int:
        hour, kind = args
        return int(w.punch(employee, ist(day, hour, 0), kind).status_code)

    with ThreadPoolExecutor(max_workers=4) as pool:
        statuses = list(pool.map(punch, [(9, "IN"), (13, "OUT"), (14, "IN"), (18, "OUT")]))

    assert statuses == [200, 200, 200, 200]
    record = w.record(employee, day).json()
    assert record["revision_count"] == 4  # one revision per manual punch, no lost updates
    assert record["gross_minutes"] == 9 * 60  # first IN 09:00 to last OUT 18:00
