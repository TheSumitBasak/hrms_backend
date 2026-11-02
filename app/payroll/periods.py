"""Payroll period derivation (§4.4, §6.6, §9.10) as pure functions.

Everything a ``payroll_periods`` row stores besides its identity is derived here from the pay
group settings, so the calendar job, the API and the tests share one definition.
"""

from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

INPUT_WINDOW_SAME_MONTH = "SAME_MONTH"
INPUT_WINDOW_OFFSET_CYCLE = "OFFSET_CYCLE"


@dataclass(frozen=True, slots=True)
class PeriodDates:
    period_start: date
    period_end: date
    tax_year_start: int
    input_window_start: date
    input_window_end: date
    input_cutoff_at: datetime  # timezone-aware UTC (UtcDateTime refuses naive values)
    target_payment_date: date
    payment_due_date: date | None


def first_of_month(day: date) -> date:
    return day.replace(day=1)


def last_of_month(day: date) -> date:
    return day.replace(day=monthrange(day.year, day.month)[1])


def add_months(first_day: date, months: int) -> date:
    index = first_day.year * 12 + (first_day.month - 1) + months
    return date(index // 12, index % 12 + 1, 1)


def tax_year_start(period_start: date) -> int:
    """PCFG-02: the tax year runs 1 April to 31 March whatever the organization's own year."""
    return period_start.year if period_start.month >= 4 else period_start.year - 1


def input_window(period_month: date, mode: str, cycle_day: int | None) -> tuple[date, date]:
    """§6.6: SAME_MONTH is the period; OFFSET_CYCLE is ``[day c of last month, day c-1]``."""
    if mode == INPUT_WINDOW_SAME_MONTH:
        return period_month, last_of_month(period_month)
    if mode != INPUT_WINDOW_OFFSET_CYCLE or cycle_day is None or not 2 <= cycle_day <= 28:
        raise ValueError("OFFSET_CYCLE needs input_cycle_start_day between 2 and 28")
    return (
        add_months(period_month, -1).replace(day=cycle_day),
        period_month.replace(day=cycle_day - 1),
    )


def cutoff_at(window_end: date, offset_days: int, timezone: str) -> datetime:
    """Window end plus ``offset_days`` at 23:59:59 organization time, as aware UTC."""
    local = datetime.combine(
        window_end + timedelta(days=offset_days), time(23, 59, 59), ZoneInfo(timezone)
    )
    return local.astimezone(UTC)


def derive_period(
    period_month: date,
    *,
    input_window_mode: str,
    input_cycle_start_day: int | None,
    input_cutoff_offset_days: int,
    target_payment_day: int,
    pay_in_period: bool,
    timezone: str,
    wage_payment_day: int | None,
) -> PeriodDates:
    """All derived columns of a payroll period.

    ``wage_payment_day`` comes from the resolved wage-payment rule (MW-10). With no rule it is
    ``None`` and ``payment_due_date`` stays ``None``: the statutory date is data, never a
    constant in code (§4.6).
    """
    if period_month.day != 1:
        raise ValueError("period_month must be the first day of a month")
    window_start, window_end = input_window(period_month, input_window_mode, input_cycle_start_day)
    pay_month = period_month if pay_in_period else add_months(period_month, 1)
    return PeriodDates(
        period_start=period_month,
        period_end=last_of_month(period_month),
        tax_year_start=tax_year_start(period_month),
        input_window_start=window_start,
        input_window_end=window_end,
        input_cutoff_at=cutoff_at(window_end, input_cutoff_offset_days, timezone),
        target_payment_date=pay_month.replace(day=target_payment_day),
        payment_due_date=(
            add_months(period_month, 1).replace(day=wage_payment_day)
            if wage_payment_day is not None
            else None
        ),
    )


def months_between(start: date, end: date) -> list[date]:
    """First-of-month dates from ``start`` to ``end`` inclusive."""
    months: list[date] = []
    current = first_of_month(start)
    while current <= first_of_month(end):
        months.append(current)
        current = add_months(current, 1)
    return months
