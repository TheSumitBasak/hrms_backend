"""§4.4 / §6.6 / §9.10 period derivation."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pytest

from app.payroll.periods import (
    PeriodDates,
    add_months,
    derive_period,
    input_window,
    months_between,
    tax_year_start,
)

IST = "Asia/Kolkata"


def derive(**overrides: Any) -> PeriodDates:
    values: dict[str, Any] = {
        "input_window_mode": "OFFSET_CYCLE",
        "input_cycle_start_day": 26,
        "input_cutoff_offset_days": 2,
        "target_payment_day": 1,
        "pay_in_period": False,
        "timezone": IST,
        "wage_payment_day": 7,
    }
    values.update(overrides)
    return derive_period(date(2026, 11, 1), **values)


def test_spec_example_for_november_2026() -> None:
    period = derive()
    assert (period.period_start, period.period_end) == (date(2026, 11, 1), date(2026, 11, 30))
    assert (period.input_window_start, period.input_window_end) == (
        date(2026, 10, 26),
        date(2026, 11, 25),
    )
    # 27 Nov 23:59:59 IST is 18:29:59 UTC (the section 4.4 example).
    assert period.input_cutoff_at == datetime(2026, 11, 27, 18, 29, 59, tzinfo=UTC)
    assert period.payment_due_date == date(2026, 12, 7)
    assert period.target_payment_date == date(2026, 12, 1)
    assert period.tax_year_start == 2026


def test_same_month_window_is_the_period() -> None:
    assert input_window(date(2026, 2, 1), "SAME_MONTH", None) == (
        date(2026, 2, 1),
        date(2026, 2, 28),
    )
    assert input_window(date(2028, 2, 1), "SAME_MONTH", None)[1] == date(2028, 2, 29)


def test_offset_window_crosses_the_year_boundary() -> None:
    assert input_window(date(2027, 1, 1), "OFFSET_CYCLE", 26) == (
        date(2026, 12, 26),
        date(2027, 1, 25),
    )


@pytest.mark.parametrize("day", [None, 1, 29])
def test_offset_cycle_day_is_validated(day: int | None) -> None:
    with pytest.raises(ValueError):
        input_window(date(2026, 11, 1), "OFFSET_CYCLE", day)


def test_pay_in_period_pays_in_the_same_month() -> None:
    period = derive(pay_in_period=True, target_payment_day=28)
    assert period.target_payment_date == date(2026, 11, 28)


def test_due_date_is_null_without_a_rule() -> None:
    assert derive(wage_payment_day=None).payment_due_date is None


@pytest.mark.parametrize(
    ("period", "expected"),
    [
        (date(2026, 3, 1), 2025),
        (date(2026, 4, 1), 2026),
        (date(2027, 1, 1), 2026),
        (date(2026, 12, 1), 2026),
    ],
)
def test_tax_year_runs_april_to_march(period: date, expected: int) -> None:
    assert tax_year_start(period) == expected


def test_month_arithmetic() -> None:
    assert add_months(date(2026, 11, 1), 2) == date(2027, 1, 1)
    assert add_months(date(2026, 1, 1), -1) == date(2025, 12, 1)
    assert months_between(date(2026, 11, 15), date(2027, 1, 3)) == [
        date(2026, 11, 1),
        date(2026, 12, 1),
        date(2027, 1, 1),
    ]


def test_period_month_must_be_a_first() -> None:
    with pytest.raises(ValueError):
        derive_period(
            date(2026, 11, 2),
            input_window_mode="SAME_MONTH",
            input_cycle_start_day=None,
            input_cutoff_offset_days=0,
            target_payment_day=1,
            pay_in_period=False,
            timezone=IST,
            wage_payment_day=None,
        )
