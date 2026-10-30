"""Leave concurrency: no over-reservation, no duplicate effects (addendum §6.11, AC-45, INT-14)."""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from typing import Any

from tests.job_architecture import if_match
from tests.leave_support import make_world, next_weekday, working_day


def _world(db_session: Any, authenticate: Any, opening: str = "2.00", **policy: Any) -> Any:
    w = make_world(db_session, authenticate)
    leave_type = w.leave_type("CL")
    w.policy(leave_type, **policy)
    manager = w.employee(first_name="Mira")
    employee = w.employee(first_name="Esha")
    w.set_manager(employee["id"], manager["id"])
    assert w.opening(employee, leave_type, opening).status_code == 201
    return w, leave_type, employee, w.self_client(employee), w.manager_client(manager)


def _ledger_matches_balance(w: Any, employee: dict[str, Any], leave_type: dict[str, Any]) -> None:
    """LEV-INV-1: the materialized sums equal the ledger sums."""
    ledger = w.hr.get(f"{w.base}/employees/{employee['id']}/leave-transactions?limit=100").json()
    credited = sum(
        Decimal(t["quantity_days"]) for t in ledger["items"] if Decimal(t["quantity_days"]) > 0
    )
    debited = -sum(
        Decimal(t["quantity_days"]) for t in ledger["items"] if Decimal(t["quantity_days"]) < 0
    )
    balance = w.balance(employee, leave_type)
    assert (Decimal(balance["credited_days"]), Decimal(balance["debited_days"])) == (
        credited,
        debited,
    )


def test_concurrent_applications_never_over_reserve(db_session: Any, authenticate: Any) -> None:
    """AC-45 widened: six one-day applications race for 2.00 days; exactly two win."""
    w, leave_type, employee, client, _ = _world(db_session, authenticate)
    days = []
    cursor = working_day(5)
    while len(days) < 6:
        if cursor.isoweekday() <= 5:
            days.append(cursor)
        cursor += timedelta(days=1)

    def submit(day: Any) -> int:
        response = w.apply(client, leave_type, day)
        return int(response.status_code)

    with ThreadPoolExecutor(max_workers=6) as pool:
        statuses = list(pool.map(submit, days))

    assert sorted(statuses) == [201, 201, 422, 422, 422, 422], statuses
    balance = w.balance(employee, leave_type)
    assert balance["reserved_days"] == "2.00"
    assert balance["available_days"] == "0.00"
    listing = client.get(f"{w.base}/me/leave-applications?status=SUBMITTED").json()
    assert len(listing["items"]) == 2
    _ledger_matches_balance(w, employee, leave_type)


def test_duplicate_submissions_for_the_same_dates_create_one_application(
    db_session: Any, authenticate: Any
) -> None:
    w, leave_type, employee, client, _ = _world(db_session, authenticate, opening="10.00")
    day = working_day(6)

    def submit(_: int) -> int:
        return int(w.apply(client, leave_type, day).status_code)

    with ThreadPoolExecutor(max_workers=4) as pool:
        statuses = list(pool.map(submit, range(4)))

    assert statuses.count(201) == 1, statuses
    assert all(s == 409 for s in statuses if s != 201), statuses  # LEAVE_OVERLAP
    assert w.balance(employee, leave_type)["reserved_days"] == "1.00"


def test_same_idempotency_key_replays_instead_of_duplicating(
    db_session: Any, authenticate: Any
) -> None:
    w, leave_type, employee, client, _ = _world(db_session, authenticate, opening="10.00")
    day = working_day(6)
    key = {"Idempotency-Key": str(uuid.uuid4())}
    body = {
        "leave_type_id": leave_type["id"],
        "start_date": day.isoformat(),
        "end_date": day.isoformat(),
    }
    url = f"{w.base}/me/leave-applications"
    first = client.post(url, json=body, headers=key)
    second = client.post(url, json=body, headers=key)
    assert first.status_code == 201 and second.status_code == 201, (first.text, second.text)
    assert first.json()["id"] == second.json()["id"]
    assert w.balance(employee, leave_type)["reserved_days"] == "1.00"


def test_concurrent_approvals_consume_once(db_session: Any, authenticate: Any) -> None:
    w, leave_type, employee, client, manager = _world(db_session, authenticate, opening="5.00")
    application = w.apply(client, leave_type, working_day(6)).json()

    def approve(_: int) -> int:
        return int(
            manager.post(
                f"{w.base}/leave-applications/{application['id']}/approve",
                json={},
                headers=if_match(application),
            ).status_code
        )

    with ThreadPoolExecutor(max_workers=3) as pool:
        statuses = list(pool.map(approve, range(3)))

    assert statuses.count(200) == 1, statuses
    balance = w.balance(employee, leave_type)
    assert (balance["posted_balance_days"], balance["reserved_days"]) == ("4.00", "0.00")
    _ledger_matches_balance(w, employee, leave_type)


def test_concurrent_cancellations_restore_once(db_session: Any, authenticate: Any) -> None:
    w, leave_type, employee, client, manager = _world(db_session, authenticate, opening="5.00")
    monday = next_weekday(1, after=7)
    application = w.apply(client, leave_type, monday, monday + timedelta(days=1)).json()
    approved = manager.post(
        f"{w.base}/leave-applications/{application['id']}/approve",
        json={},
        headers=if_match(application),
    ).json()

    def cancel(_: int) -> int:
        return int(
            client.post(
                f"{w.base}/me/leave-applications/{approved['id']}/cancel",
                json={"scope": "FULL", "reason": "Plans were cancelled by the client"},
                headers=if_match(approved),
            ).status_code
        )

    with ThreadPoolExecutor(max_workers=3) as pool:
        statuses = list(pool.map(cancel, range(3)))

    assert statuses.count(200) == 1, statuses
    assert w.balance(employee, leave_type)["posted_balance_days"] == "5.00"
    _ledger_matches_balance(w, employee, leave_type)
