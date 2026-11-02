"""Payroll addendum §4 / §15.2: settings, pay groups and the period calendar."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import update

from app.core.permissions import Role
from app.models.organization import Organization
from app.models.payroll import PayrollPeriod
from app.payroll.periods import add_months, first_of_month
from tests.factories import create_organization, create_user

GO_LIVE = first_of_month(datetime.now(UTC).date()).replace(day=1)


def _login(db_session: Any, authenticate: Any, *roles: Role) -> tuple[Any, Any]:
    organization = create_organization(db_session)
    user = create_user(db_session, grants=[(organization.id, role) for role in roles])
    db_session.commit()
    return organization, authenticate(user)


def _settings_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "enable_payroll_module": True,
        "payroll_go_live_period": GO_LIVE.isoformat(),
    }
    body.update(overrides)
    return body


def _enable(client: Any, organization_id: Any, **overrides: Any) -> Any:
    response = client.put(
        f"/api/v1/orgs/{organization_id}/payroll-settings", json=_settings_body(**overrides)
    )
    assert response.status_code == 200, response.text
    return response


class TestPayrollSettings:
    def test_first_put_creates_settings_and_enables_the_module(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization, client = _login(db_session, authenticate, Role.PAYROLL_ADMINISTRATOR)
        base = f"/api/v1/orgs/{organization.id}"
        assert client.get(f"{base}/payroll-settings").status_code == 404

        response = _enable(client, organization.id)
        body = response.json()
        assert response.headers["ETag"] == f'"{body["row_version"]}"'
        assert body["enable_payroll_module"] is True
        assert body["enable_payroll_alumni_access"] is True
        assert body["deduction_limit_percent"] == "50.0000"
        # Go-live in a month other than April means opening balances are needed (§4.2).
        assert body["opening_ytd_required"] is (GO_LIVE.month != 4)

        again = client.get(f"{base}/payroll-settings")
        assert again.status_code == 200
        assert again.json()["id"] == body["id"]

    def test_replace_requires_the_current_etag(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization, client = _login(db_session, authenticate, Role.PAYROLL_ADMINISTRATOR)
        first = _enable(client, organization.id).json()
        stale = client.put(
            f"/api/v1/orgs/{organization.id}/payroll-settings",
            json=_settings_body(lop_lookback_periods=6),
            headers={"If-Match": str(first["row_version"] + 5)},
        )
        assert stale.status_code in {409, 412}, stale.text
        fresh = client.put(
            f"/api/v1/orgs/{organization.id}/payroll-settings",
            json=_settings_body(lop_lookback_periods=6),
            headers={"If-Match": str(first["row_version"])},
        )
        assert fresh.status_code == 200, fresh.text
        assert fresh.json()["lop_lookback_periods"] == 6

    def test_a_json_number_is_refused_for_a_rate(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization, client = _login(db_session, authenticate, Role.PAYROLL_ADMINISTRATOR)
        response = client.put(
            f"/api/v1/orgs/{organization.id}/payroll-settings",
            json=_settings_body(deduction_limit_percent=50),
        )
        assert response.status_code == 422
        assert "MONEY_MUST_BE_STRING" in response.text

    def test_go_live_must_be_a_first_of_month(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization, client = _login(db_session, authenticate, Role.PAYROLL_ADMINISTRATOR)
        response = client.put(
            f"/api/v1/orgs/{organization.id}/payroll-settings",
            json=_settings_body(payroll_go_live_period="2026-11-02"),
        )
        assert response.status_code == 422

    def test_a_non_inr_organization_cannot_enable_payroll(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization, client = _login(db_session, authenticate, Role.PAYROLL_ADMINISTRATOR)
        db_session.execute(
            update(Organization)
            .where(Organization.id == organization.id)
            .values(default_currency="USD")
        )
        db_session.commit()
        response = client.put(
            f"/api/v1/orgs/{organization.id}/payroll-settings", json=_settings_body()
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "PAYROLL_CURRENCY_NOT_SUPPORTED"

    def test_roles_without_payroll_access_are_refused(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization, hr = _login(db_session, authenticate, Role.HR_ADMIN)
        base = f"/api/v1/orgs/{organization.id}"
        assert hr.get(f"{base}/payroll-settings").status_code == 403
        assert hr.put(f"{base}/payroll-settings", json=_settings_body()).status_code == 403

    def test_a_processor_can_read_but_not_configure(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization, processor = _login(db_session, authenticate, Role.PAYROLL_PROCESSOR)
        base = f"/api/v1/orgs/{organization.id}"
        assert processor.put(f"{base}/payroll-settings", json=_settings_body()).status_code == 403
        assert processor.get(f"{base}/pay-groups").status_code == 200


class TestPayGroupsAndPeriods:
    def test_group_creation_is_refused_until_payroll_is_enabled(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization, client = _login(db_session, authenticate, Role.PAYROLL_ADMINISTRATOR)
        response = client.post(
            f"/api/v1/orgs/{organization.id}/pay-groups",
            json={"pay_group_code": "MAIN", "pay_group_name": "Monthly staff"},
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "MODULE_NOT_ENABLED"

    def test_group_creates_its_calendar_and_codes_are_unique(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization, client = _login(db_session, authenticate, Role.PAYROLL_ADMINISTRATOR)
        base = f"/api/v1/orgs/{organization.id}"
        _enable(client, organization.id)
        created = client.post(
            f"{base}/pay-groups",
            json={
                "pay_group_code": "MAIN",
                "pay_group_name": "Monthly staff",
                "input_window_mode": "OFFSET_CYCLE",
                "input_cycle_start_day": 26,
                "input_cutoff_offset_days": 2,
            },
        )
        assert created.status_code == 201, created.text
        group = created.json()
        assert created.headers["ETag"] == f'"{group["row_version"]}"'

        duplicate = client.post(
            f"{base}/pay-groups", json={"pay_group_code": "main", "pay_group_name": "Again"}
        )
        assert duplicate.status_code in {409, 422}  # lower-case fails the code pattern first

        periods = client.get(f"{base}/payroll-periods", params={"pay_group_id": group["id"]})
        assert periods.status_code == 200
        rows = periods.json()
        months = [date.fromisoformat(row["period_month"]) for row in rows]
        assert months[0] == GO_LIVE
        assert months[-1] == add_months(first_of_month(GO_LIVE), 13)
        assert months == sorted(set(months))  # contiguous months, no duplicates

        first = rows[0]
        assert first["input_window_start"] == add_months(GO_LIVE, -1).replace(day=26).isoformat()
        assert first["input_window_end"] == GO_LIVE.replace(day=25).isoformat()
        assert first["payment_due_date"] is None  # no published wage-payment rule yet
        assert first["status"] == "OPEN"

    def test_calendar_generation_is_idempotent(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization, client = _login(db_session, authenticate, Role.PAYROLL_ADMINISTRATOR)
        base = f"/api/v1/orgs/{organization.id}"
        _enable(client, organization.id)
        client.post(f"{base}/pay-groups", json={"pay_group_code": "MAIN", "pay_group_name": "Main"})
        before = client.get(f"{base}/payroll-periods").json()
        settings = client.get(f"{base}/payroll-settings").json()
        again = client.put(  # re-running the generator must add nothing
            f"{base}/payroll-settings",
            json=_settings_body(),
            headers={"If-Match": str(settings["row_version"])},
        )
        assert again.status_code == 200, again.text
        after = client.get(f"{base}/payroll-periods").json()
        assert [row["id"] for row in after] == [row["id"] for row in before]

    def test_cycle_change_validates_the_pair_and_keeps_existing_periods(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization, client = _login(db_session, authenticate, Role.PAYROLL_ADMINISTRATOR)
        base = f"/api/v1/orgs/{organization.id}"
        _enable(client, organization.id)
        group = client.post(
            f"{base}/pay-groups", json={"pay_group_code": "MAIN", "pay_group_name": "Main"}
        ).json()
        before = client.get(f"{base}/payroll-periods").json()

        bad = client.patch(
            f"{base}/pay-groups/{group['id']}",
            json={"input_window_mode": "OFFSET_CYCLE"},
            headers={"If-Match": str(group["row_version"])},
        )
        assert bad.status_code == 422

        good = client.patch(
            f"{base}/pay-groups/{group['id']}",
            json={"input_window_mode": "OFFSET_CYCLE", "input_cycle_start_day": 20},
            headers={"If-Match": str(group["row_version"])},
        )
        assert good.status_code == 200, good.text
        assert good.json()["input_cycle_start_day"] == 20
        after = client.get(f"{base}/payroll-periods").json()
        assert [row["input_window_start"] for row in after] == [
            row["input_window_start"] for row in before
        ]

    def test_target_payment_date_is_editable_while_open_only(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        organization, client = _login(db_session, authenticate, Role.PAYROLL_ADMINISTRATOR)
        base = f"/api/v1/orgs/{organization.id}"
        _enable(client, organization.id)
        client.post(f"{base}/pay-groups", json={"pay_group_code": "MAIN", "pay_group_name": "Main"})
        period = client.get(f"{base}/payroll-periods").json()[0]

        updated = client.patch(
            f"{base}/payroll-periods/{period['id']}",
            json={"target_payment_date": add_months(GO_LIVE, 1).replace(day=3).isoformat()},
            headers={"If-Match": str(period["row_version"])},
        )
        assert updated.status_code == 200, updated.text
        assert (
            updated.json()["target_payment_date"]
            == add_months(GO_LIVE, 1).replace(day=3).isoformat()
        )

        db_session.execute(
            update(PayrollPeriod)
            .where(PayrollPeriod.period_month == GO_LIVE)
            .values(status="CLOSED")
        )
        db_session.commit()
        closed = client.patch(
            f"{base}/payroll-periods/{period['id']}",
            json={"target_payment_date": add_months(GO_LIVE, 1).replace(day=4).isoformat()},
            headers={"If-Match": str(updated.json()["row_version"])},
        )
        assert closed.status_code == 422

    def test_periods_do_not_cross_organizations(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        first_org, first = _login(db_session, authenticate, Role.PAYROLL_ADMINISTRATOR)
        second_org, second = _login(db_session, authenticate, Role.PAYROLL_ADMINISTRATOR)
        _enable(first, first_org.id)
        first.post(
            f"/api/v1/orgs/{first_org.id}/pay-groups",
            json={"pay_group_code": "MAIN", "pay_group_name": "Main"},
        )
        period = first.get(f"/api/v1/orgs/{first_org.id}/payroll-periods").json()[0]
        _enable(second, second_org.id)
        foreign = second.patch(
            f"/api/v1/orgs/{second_org.id}/payroll-periods/{period['id']}",
            json={"target_payment_date": GO_LIVE.isoformat()},
        )
        assert foreign.status_code == 404
