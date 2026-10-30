"""LEAVE_ACCRUAL and LEAVE_YEAR_END (addendum §6.6, §6.7, AC-47, AC-48, AC-58)."""

from __future__ import annotations

from datetime import date
from typing import Any

from app.core.errors import ErrorCode
from tests.leave_support import make_world

MONTHLY = {
    "allocation_method": "PERIODIC_ACCRUAL",
    "annual_entitlement_days": "10.00",
    "accrual_frequency": "MONTHLY",
    "accrual_timing": "PERIOD_END",
    "joining_cutoff_day": 15,
}


def _accruals(w: Any, employee: dict[str, Any]) -> list[tuple[str, str]]:
    return [
        (t["effective_date"], t["quantity_days"])
        for t in w.ledger(employee, 2026)
        if t["transaction_type"] == "ACCRUAL"
    ]


class TestAccrual:
    def test_monthly_accrual_follows_the_cumulative_formula_and_reruns_are_noops(
        self, db_session: Any, authenticate: Any
    ) -> None:
        w = make_world(db_session, authenticate)
        leave_type = w.leave_type("EL")
        w.policy(leave_type, **MONTHLY)
        joiner = w.employee(joining_date="2026-03-10")  # after the Jan/Feb cutoff dates
        first = w.run_job("LEAVE_ACCRUAL", date(2026, 5, 31))
        assert first.status_code == 201, first.text
        assert first.json()["status"] == "SUCCEEDED"
        assert _accruals(w, joiner) == [
            ("2026-03-31", "0.83"),
            ("2026-04-30", "0.84"),
            ("2026-05-31", "0.83"),
        ]
        # A second manual run for the same date, and a later one that only adds June.
        assert w.run_job("LEAVE_ACCRUAL", date(2026, 5, 31)).status_code == 201
        assert len(_accruals(w, joiner)) == 3
        w.run_job("LEAVE_ACCRUAL", date(2026, 6, 30))
        assert len(_accruals(w, joiner)) == 4
        balance = w.balance(joiner, leave_type, 2026)
        assert balance["posted_balance_days"] == "3.33"  # 10 * 4 / 12, self-correcting

    def test_cap_forfeits_the_excess_and_records_the_entitled_amount(
        self, db_session: Any, authenticate: Any
    ) -> None:
        w = make_world(db_session, authenticate)
        leave_type = w.leave_type("EL")
        w.policy(leave_type, max_balance_days="1.00", **MONTHLY)
        joiner = w.employee(joining_date="2026-03-10")
        w.run_job("LEAVE_ACCRUAL", date(2026, 5, 31))
        rows = [t for t in w.ledger(joiner, 2026) if t["transaction_type"] == "ACCRUAL"]
        assert [(t["quantity_days"], t["entitled_quantity_days"]) for t in rows] == [
            ("0.83", "0.83"),
            ("0.17", "0.84"),
            ("0.00", "0.83"),
        ]
        assert w.balance(joiner, leave_type, 2026)["posted_balance_days"] == "1.00"

    def test_annual_upfront_prorates_by_joining_month(
        self, db_session: Any, authenticate: Any
    ) -> None:
        w = make_world(db_session, authenticate)
        leave_type = w.leave_type("AL")
        w.policy(
            leave_type,
            allocation_method="ANNUAL_UPFRONT",
            annual_entitlement_days="12.00",
            upfront_proration="MONTHLY",
            joining_cutoff_day=15,
        )
        joiner = w.employee(joining_date="2026-03-20")  # day 20 > cutoff 15 -> 9 months
        w.run_job("LEAVE_ACCRUAL", date(2026, 4, 1))
        rows = [t for t in w.ledger(joiner, 2026) if t["transaction_type"] == "ALLOCATION"]
        assert [t["quantity_days"] for t in rows] == ["9.00"]

    def test_only_a_permitted_user_can_run_jobs_and_future_dates_are_refused(
        self, db_session: Any, authenticate: Any
    ) -> None:
        w = make_world(db_session, authenticate)
        denied = w.manager_client(w.employee()).post(
            f"{w.base}/leave-processing/runs",
            json={"job_code": "LEAVE_ACCRUAL", "as_of_date": "2026-05-31"},
            headers={"Idempotency-Key": "7b0c1d3e-0000-4000-8000-000000000001"},
        )
        assert denied.status_code == 403
        future = w.run_job("LEAVE_ACCRUAL", date(2099, 1, 1))
        assert future.status_code == 422


class TestYearEnd:
    def _world(self, db_session: Any, authenticate: Any) -> Any:
        w = make_world(db_session, authenticate, leave_tracking_start_date=date(2025, 1, 1))
        leave_type = w.leave_type("EL")
        w.policy(
            leave_type,
            effective_from="2025-01-01",
            carry_forward_enabled=True,
            max_carry_forward_days="10.00",
            max_backdate_days=365,
        )
        return w, leave_type

    def test_carry_forward_and_lapse_post_once(self, db_session: Any, authenticate: Any) -> None:
        w, leave_type = self._world(db_session, authenticate)
        employee = w.employee(joining_date="2025-01-06")
        assert w.opening(employee, leave_type, "14.50", year_number=2025).status_code == 201

        first = w.run_job("LEAVE_YEAR_END", date(2026, 1, 2))
        assert first.status_code == 201 and first.json()["status"] == "SUCCEEDED", first.text
        kinds = {t["transaction_type"]: t["quantity_days"] for t in w.ledger(employee, 2025)}
        assert kinds["CARRY_FORWARD_OUT"] == "-10.00" and kinds["LAPSE"] == "-4.50"
        assert w.balance(employee, leave_type, 2025)["closure_status"] == "CLOSED"
        assert w.balance(employee, leave_type, 2025)["posted_balance_days"] == "0.00"
        carried = [
            t for t in w.ledger(employee, 2026) if t["transaction_type"] == "CARRY_FORWARD_IN"
        ]
        assert [t["quantity_days"] for t in carried] == ["10.00"]

        before = len(w.ledger(employee))
        w.run_job("LEAVE_YEAR_END", date(2026, 1, 2))
        assert len(w.ledger(employee)) == before  # AC-48: no second set of postings

    def test_pending_application_defers_the_closure_until_it_is_decided(
        self, db_session: Any, authenticate: Any
    ) -> None:
        w, leave_type = self._world(db_session, authenticate)
        manager = w.employee(first_name="Mira", joining_date="2025-01-06")
        employee = w.employee(joining_date="2025-01-06")
        w.set_manager(employee["id"], manager["id"])
        w.opening(employee, leave_type, "5.00", year_number=2025)
        client = w.self_client(employee)
        pending = w.apply(client, leave_type, date(2025, 12, 1))
        assert pending.status_code == 201, pending.text

        deferred = w.run_job("LEAVE_YEAR_END", date(2026, 1, 2))
        assert deferred.json()["skipped_count"] == 1
        assert w.balance(employee, leave_type, 2025)["closure_status"] == "OPEN"

        withdrawn = client.post(
            f"{w.base}/me/leave-applications/{pending.json()['id']}/withdraw",
            json={},
            headers={"If-Match": str(pending.json()["row_version"])},
        )
        assert withdrawn.status_code == 200, withdrawn.text
        w.run_job("LEAVE_YEAR_END", date(2026, 1, 2))
        assert w.balance(employee, leave_type, 2025)["closure_status"] == "CLOSED"

    def test_posting_into_a_closed_year_is_refused(
        self, db_session: Any, authenticate: Any
    ) -> None:
        w, leave_type = self._world(db_session, authenticate)
        employee = w.employee(joining_date="2025-01-06")
        w.opening(employee, leave_type, "3.00", year_number=2025)
        w.run_job("LEAVE_YEAR_END", date(2026, 1, 2))
        late = w.hr.post(
            f"{w.base}/employees/{employee['id']}/leave-transactions",
            json={
                "transaction_type": "ADJUSTMENT_CREDIT",
                "leave_type_id": leave_type["id"],
                "leave_year_start": "2025-01-01",
                "quantity_days": "1.00",
                "effective_date": "2025-06-01",
                "reason": "Late credit for a missed compensatory day",
            },
            headers={"Idempotency-Key": "7b0c1d3e-0000-4000-8000-000000000002"},
        )
        assert late.status_code == 422
        assert late.json()["error"]["code"] == ErrorCode.LEAVE_YEAR_CLOSED.value
