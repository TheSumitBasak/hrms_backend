"""Leave formulas against the addendum's worked examples (§6.1, §6.6, §6.9)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.services.leave_rules import (
    Half,
    accrual_periods,
    apply_cap,
    cumulative_accrual,
    evaluate_slots,
    leave_year_label,
    leave_year_start,
    round_days,
    upfront_entitlement,
)

D = Decimal


def test_leave_year_boundaries_and_labels() -> None:
    assert leave_year_start(date(2027, 3, 31), 4) == date(2026, 4, 1)
    assert leave_year_start(date(2027, 4, 1), 4) == date(2027, 4, 1)
    assert leave_year_start(date(2027, 12, 31), 1) == date(2027, 1, 1)
    assert leave_year_label(date(2027, 1, 1)) == "2027"
    assert leave_year_label(date(2027, 4, 1)) == "2027-28"


@pytest.mark.parametrize(
    ("mode", "value", "expected"),
    [
        ("TWO_DECIMALS", "0.825", "0.83"),
        ("NEAREST_HALF_DAY", "1.25", "1.50"),
        ("NEAREST_HALF_DAY", "1.24", "1.00"),
        ("DOWN_TO_HALF_DAY", "1.99", "1.50"),
        ("DOWN_TO_HALF_DAY", "2.00", "2.00"),
    ],
)
def test_rounding(mode: str, value: str, expected: str) -> None:
    assert round_days(D(value), mode) == D(expected)


def test_monthly_accrual_worked_example_is_self_correcting() -> None:
    posted, prior = [], D("0.00")
    for count in range(3):
        entitled = cumulative_accrual(D("10.00"), 12, count, prior, "TWO_DECIMALS")
        posted.append(entitled)
        prior += entitled
    assert posted == [D("0.83"), D("0.84"), D("0.83")]


def test_cap_forfeits_without_recredit() -> None:
    assert apply_cap(D("0.83"), D("1.00"), D("0.50")) == D("0.50")
    assert apply_cap(D("0.83"), D("1.00"), D("1.20")) == D("0.00")
    assert apply_cap(D("0.83"), None, D("99")) == D("0.83")


def test_accrual_periods_and_cutoff_clamped_to_month_length() -> None:
    periods = accrual_periods(date(2027, 1, 1), "MONTHLY", 31)
    assert len(periods) == 12
    assert periods[1].cutoff == date(2027, 2, 28)
    assert periods[0].last_day == date(2027, 1, 31)
    assert accrual_periods(date(2027, 4, 1), "QUARTERLY", 15)[3].start == date(2028, 1, 1)


def test_upfront_proration_worked_example() -> None:
    # joined 20 March, cutoff 15: March-December is 10 months, minus 1 -> 9 -> 9.00
    assert upfront_entitlement(
        D("12.00"), "MONTHLY", date(2027, 3, 20), date(2027, 1, 1), 15, "TWO_DECIMALS"
    ) == D("9.00")
    assert upfront_entitlement(
        D("12.00"), "MONTHLY", date(2027, 3, 10), date(2027, 1, 1), 15, "TWO_DECIMALS"
    ) == D("10.00")
    assert upfront_entitlement(
        D("12.00"), "NONE", date(2027, 3, 20), date(2027, 1, 1), 15, "TWO_DECIMALS"
    ) == D("12.00")


def _weekend_off(day: date) -> str:
    return "WEEKLY_OFF" if day.isoweekday() >= 6 else "WORKING_DAY"


@pytest.mark.parametrize(("count_off", "expected"), [(False, "1.50"), (True, "3.50")])
def test_duration_worked_example(count_off: bool, expected: str) -> None:
    slots, days = evaluate_slots(
        date(2026, 11, 13),
        Half.FIRST_HALF,
        date(2026, 11, 16),
        Half.FIRST_HALF,
        _weekend_off,
        count_weekly_offs=count_off,
        count_holidays=False,
    )
    assert days == D(expected)
    assert len(slots) == 7  # Fri 2 + Sat 2 + Sun 2 + Mon FIRST_HALF only


def test_holiday_takes_count_holidays_flag() -> None:
    _, days = evaluate_slots(
        date(2026, 1, 1),
        Half.FIRST_HALF,
        date(2026, 1, 1),
        Half.SECOND_HALF,
        lambda _d: "HOLIDAY",
        count_weekly_offs=True,
        count_holidays=False,
    )
    assert days == D("0.00")
