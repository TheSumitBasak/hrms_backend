"""Leave types, policies, resolution and the ledger (addendum §6.2-§6.5, §6.13)."""

from __future__ import annotations

import uuid
from typing import Any

from app.core.errors import ErrorCode
from app.core.permissions import Role
from tests.job_architecture import if_match
from tests.leave_support import make_world, today

YEAR_START = f"{today().year}-01-01"


def _code(response: Any) -> str:
    return str(response.json()["error"]["code"])


class TestLeaveTypesAndPolicies:
    def test_type_crud_duplicate_and_status(self, db_session: Any, authenticate: Any) -> None:
        w = make_world(db_session, authenticate)
        created = w.leave_type("CL")
        assert created["status"] == "ACTIVE"

        duplicate = w.hr.post(
            f"{w.base}/leave-types", json={"leave_type_code": "cl", "leave_type_name": "Dup"}
        )
        assert duplicate.status_code in (409, 422), duplicate.text

        stale = w.hr.patch(
            f"{w.base}/leave-types/{created['id']}",
            json={"leave_type_name": "Casual leave"},
            headers={"If-Match": "99"},
        )
        assert stale.status_code == 412, stale.text
        ok = w.hr.patch(
            f"{w.base}/leave-types/{created['id']}",
            json={"leave_type_name": "Casual leave"},
            headers=if_match(created),
        )
        assert ok.status_code == 200 and ok.json()["leave_type_name"] == "Casual leave"

        off = w.hr.post(
            f"{w.base}/leave-types/{created['id']}/deactivate",
            json={"reason": "Retired by policy review"},
        )
        assert off.status_code == 200 and off.json()["status"] == "INACTIVE"

    def test_category_locks_once_a_policy_uses_the_type(
        self, db_session: Any, authenticate: Any
    ) -> None:
        w = make_world(db_session, authenticate)
        leave_type = w.leave_type("CL")
        w.policy(leave_type)
        current = w.hr.get(f"{w.base}/leave-types/{leave_type['id']}").json()
        locked = w.hr.patch(
            f"{w.base}/leave-types/{leave_type['id']}",
            json={"tracks_balance": False},
            headers=if_match(current),
        )
        assert locked.status_code == 422, locked.text

    def test_optional_holiday_must_track_balance(self, db_session: Any, authenticate: Any) -> None:
        w = make_world(db_session, authenticate)
        response = w.hr.post(
            f"{w.base}/leave-types",
            json={
                "leave_type_code": "OH",
                "leave_type_name": "Optional holiday",
                "leave_category": "OPTIONAL_HOLIDAY",
                "tracks_balance": False,
            },
        )
        assert response.status_code == 422, response.text

    def test_publish_requires_complete_version_and_one_draft(
        self, db_session: Any, authenticate: Any
    ) -> None:
        w = make_world(db_session, authenticate)
        leave_type = w.leave_type("EL")
        policy = w.policy(
            leave_type,
            publish=False,
            effective_from=None,
            allocation_method="PERIODIC_ACCRUAL",
        )
        draft = policy["versions"][0]
        incomplete = w.hr.post(
            f"{w.base}/leave-policies/{policy['id']}/versions/{draft['id']}/publish",
            headers=if_match(draft),
        )
        assert incomplete.status_code == 422, incomplete.text

        second = w.hr.post(f"{w.base}/leave-policies/{policy['id']}/versions", json={})
        assert _code(second) == ErrorCode.LEAVE_POLICY_DRAFT_EXISTS.value

    def test_conflicting_policies_for_one_type_are_refused(
        self, db_session: Any, authenticate: Any
    ) -> None:
        w = make_world(db_session, authenticate)
        leave_type = w.leave_type("CL")
        w.policy(leave_type, code="A")
        other = w.policy(leave_type, code="B", publish=False)
        draft = other["versions"][0]
        response = w.hr.post(
            f"{w.base}/leave-policies/{other['id']}/versions/{draft['id']}/publish",
            headers=if_match(draft),
        )
        assert response.status_code == 409
        assert _code(response) == ErrorCode.LEAVE_POLICY_CONFLICT.value

    def test_resolution_prefers_the_more_specific_policy(
        self, db_session: Any, authenticate: Any
    ) -> None:
        w = make_world(db_session, authenticate)
        leave_type = w.leave_type("CL")
        general = w.policy(leave_type, code="GEN")
        specific = w.policy(
            leave_type, code="DEPT", applies_department_id=w.arch.ids["department_id"]
        )
        employee = w.employee()
        resolved = w.hr.post(
            f"{w.base}/leave-policies/resolve",
            json={
                "employee_id": employee["id"],
                "leave_type_id": leave_type["id"],
                "date": "2026-06-01",
            },
        )
        assert resolved.status_code == 200, resolved.text
        assert resolved.json()["policy_id"] == specific["id"] != general["id"]

        before_effective = w.hr.post(
            f"{w.base}/leave-policies/resolve",
            json={
                "employee_id": employee["id"],
                "leave_type_id": leave_type["id"],
                "date": "2025-06-01",
            },
        )
        assert _code(before_effective) == ErrorCode.LEAVE_TYPE_NOT_APPLICABLE.value

    def test_mutations_need_the_module_enabled_and_the_permission(
        self, db_session: Any, authenticate: Any
    ) -> None:
        w = make_world(db_session, authenticate, enable_leave_module=False)
        response = w.hr.post(
            f"{w.base}/leave-types", json={"leave_type_code": "CL", "leave_type_name": "Casual"}
        )
        assert response.status_code == 403
        assert _code(response) == ErrorCode.MODULE_NOT_ENABLED.value

        w2 = make_world(db_session, authenticate)
        manager = w2.manager_client(w2.employee())
        denied = manager.post(
            f"{w2.base}/leave-types", json={"leave_type_code": "CL", "leave_type_name": "Casual"}
        )
        assert denied.status_code == 403
        assert _code(denied) == ErrorCode.PERMISSION_DENIED.value


class TestLedger:
    def test_opening_balance_posts_once_and_updates_the_balance(
        self, db_session: Any, authenticate: Any
    ) -> None:
        w = make_world(db_session, authenticate)
        leave_type = w.leave_type("CL")
        w.policy(leave_type)
        employee = w.employee()

        first = w.opening(employee, leave_type, "12.00")
        assert first.status_code == 201, first.text
        assert first.json()["balance"]["available_days"] == "12.00"

        again = w.opening(employee, leave_type, "5.00")
        assert again.status_code == 409
        assert _code(again) == ErrorCode.OPENING_BALANCE_EXISTS.value
        assert w.balance(employee, leave_type)["posted_balance_days"] == "12.00"

    def test_idempotent_adjustment_replays_without_double_posting(
        self, db_session: Any, authenticate: Any
    ) -> None:
        w = make_world(db_session, authenticate)
        leave_type = w.leave_type("CL")
        w.policy(leave_type)
        employee = w.employee()
        w.opening(employee, leave_type, "10.00")
        key = {"Idempotency-Key": str(uuid.uuid4())}
        body = {
            "transaction_type": "ADJUSTMENT_CREDIT",
            "leave_type_id": leave_type["id"],
            "leave_year_start": YEAR_START,
            "quantity_days": "2.00",
            "effective_date": f"{today().year}-01-02",
            "reason": "Compensatory credit for weekend support work",
        }
        url = f"{w.base}/employees/{employee['id']}/leave-transactions"
        first = w.hr.post(url, json=body, headers=key)
        replay = w.hr.post(url, json=body, headers=key)
        assert first.status_code == 201 and replay.status_code == 201
        assert first.json()["transaction"]["id"] == replay.json()["transaction"]["id"]
        assert w.balance(employee, leave_type)["posted_balance_days"] == "12.00"

    def test_debit_below_zero_needs_the_warning_acknowledged(
        self, db_session: Any, authenticate: Any
    ) -> None:
        w = make_world(db_session, authenticate)
        leave_type = w.leave_type("CL")
        w.policy(leave_type)
        employee = w.employee()
        w.opening(employee, leave_type, "1.00")
        body = {
            "transaction_type": "ADJUSTMENT_DEBIT",
            "leave_type_id": leave_type["id"],
            "leave_year_start": YEAR_START,
            "quantity_days": "-3.00",
            "effective_date": f"{today().year}-01-03",
            "reason": "Correction of an over-granted carry-in balance",
        }
        url = f"{w.base}/employees/{employee['id']}/leave-transactions"
        refused = w.hr.post(url, json=body, headers={"Idempotency-Key": str(uuid.uuid4())})
        assert refused.status_code == 422
        assert _code(refused) == ErrorCode.WARNINGS_REQUIRE_ACKNOWLEDGEMENT.value
        body["acknowledged_warnings"] = ["W-LEAVE-NEGATIVE-AFTER-ADJUSTMENT"]
        accepted = w.hr.post(url, json=body, headers={"Idempotency-Key": str(uuid.uuid4())})
        assert accepted.status_code == 201, accepted.text
        assert w.balance(employee, leave_type)["posted_balance_days"] == "-2.00"

    def test_reversal_once_only_and_ledger_sums_match_the_balance(
        self, db_session: Any, authenticate: Any
    ) -> None:
        w = make_world(db_session, authenticate)
        leave_type = w.leave_type("CL")
        w.policy(leave_type)
        employee = w.employee()
        opening = w.opening(employee, leave_type, "8.00").json()["transaction"]
        url = f"{w.base}/employees/{employee['id']}/leave-transactions/{opening['id']}/reverse"
        reversed_ = w.hr.post(
            url, json={"reason": "Opening balance was keyed against the wrong person"}
        )
        assert reversed_.status_code == 201, reversed_.text
        assert reversed_.json()["transaction"]["quantity_days"] == "-8.00"
        again = w.hr.post(
            url, json={"reason": "Opening balance was keyed against the wrong person"}
        )
        assert again.status_code == 409
        assert _code(again) == ErrorCode.LEAVE_TRANSACTION_ALREADY_REVERSED.value

        ledger = w.hr.get(f"{w.base}/employees/{employee['id']}/leave-transactions").json()
        assert [t["transaction_type"] for t in ledger["items"]] == ["OPENING_BALANCE", "REVERSAL"]
        balance = w.balance(employee, leave_type)
        assert (balance["credited_days"], balance["debited_days"]) == ("8.00", "8.00")

    def test_a_user_cannot_adjust_their_own_balance(
        self, db_session: Any, authenticate: Any
    ) -> None:
        w = make_world(db_session, authenticate)
        leave_type = w.leave_type("CL")
        w.policy(leave_type)
        employee = w.employee()
        hr_self, _ = w.client_for(employee["id"], Role.HR_ADMIN)
        year = YEAR_START
        response = hr_self.post(
            f"{w.base}/employees/{employee['id']}/leave-transactions",
            json={
                "transaction_type": "OPENING_BALANCE",
                "leave_type_id": leave_type["id"],
                "leave_year_start": year,
                "quantity_days": "30.00",
                "effective_date": year,
                "reason": "Granting myself a very generous balance",
            },
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
        assert response.status_code == 403
        assert _code(response) == ErrorCode.SELF_ADJUSTMENT_NOT_ALLOWED.value

    def test_transactions_of_another_organization_are_invisible(
        self, db_session: Any, authenticate: Any
    ) -> None:
        w1 = make_world(db_session, authenticate)
        w2 = make_world(db_session, authenticate)
        leave_type = w1.leave_type("CL")
        w1.policy(leave_type)
        employee = w1.employee()
        w1.opening(employee, leave_type, "4.00")
        # w2's HR has no grant in w1 (403); w1's employee id under w2's own org is a 404.
        assert w2.hr.get(f"{w1.base}/employees/{employee['id']}/leave-balances").status_code == 403
        cross = w2.hr.get(f"{w2.base}/employees/{employee['id']}/leave-balances")
        assert cross.status_code == 404
