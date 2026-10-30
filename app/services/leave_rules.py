"""Pure leave rules (addendum §6.1, §6.6, §6.9). No database access, no clock."""

from __future__ import annotations

import calendar
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal
from enum import StrEnum

TWO = Decimal("0.01")
HALF = Decimal("0.50")
FREQUENCY_PERIODS = {"MONTHLY": 12, "QUARTERLY": 4, "HALF_YEARLY": 2}


class Half(StrEnum):
    FIRST_HALF = "FIRST_HALF"
    SECOND_HALF = "SECOND_HALF"


# ----------------------------------------------------------------------- leave year (LEV-01)


def _add_months(day: date, months: int) -> date:
    index = day.year * 12 + day.month - 1 + months
    return date(index // 12, index % 12 + 1, 1)


def leave_year_start(day: date, start_month: int) -> date:
    """First day of the leave year containing ``day``."""
    year = day.year if day.month >= start_month else day.year - 1
    return date(year, start_month, 1)


def next_leave_year_start(year_start: date) -> date:
    return date(year_start.year + 1, year_start.month, 1)


def leave_year_label(year_start: date) -> str:
    if year_start.month == 1:
        return str(year_start.year)
    return f"{year_start.year}-{(year_start.year + 1) % 100:02d}"


# ----------------------------------------------------------------------- rounding R(x)


def round_days(value: Decimal, mode: str) -> Decimal:
    """§6.6 R(x). Half-up everywhere; DOWN_TO_HALF_DAY floors."""
    if mode == "TWO_DECIMALS":
        return value.quantize(TWO, rounding=ROUND_HALF_UP)
    if mode == "NEAREST_HALF_DAY":
        return ((value / HALF).quantize(Decimal(1), rounding=ROUND_HALF_UP) * HALF).quantize(TWO)
    if mode == "DOWN_TO_HALF_DAY":
        return ((value / HALF).quantize(Decimal(1), rounding=ROUND_FLOOR) * HALF).quantize(TWO)
    raise ValueError(f"unknown rounding mode {mode!r}")


# ----------------------------------------------------------------------- accrual (§6.6)


@dataclass(frozen=True, slots=True)
class AccrualPeriod:
    number: int
    start: date
    end_exclusive: date
    cutoff: date

    @property
    def last_day(self) -> date:
        return self.end_exclusive - timedelta(days=1)


def accrual_periods(year_start: date, frequency: str, cutoff_day: int) -> list[AccrualPeriod]:
    count = FREQUENCY_PERIODS[frequency]
    step = 12 // count
    periods = []
    for k in range(1, count + 1):
        start = _add_months(year_start, (k - 1) * step)
        end = _add_months(year_start, k * step)
        last = calendar.monthrange(start.year, start.month)[1]
        periods.append(AccrualPeriod(k, start, end, start.replace(day=min(cutoff_day, last))))
    return periods


def cumulative_accrual(
    annual_days: Decimal,
    periods_per_year: int,
    accrued_count: int,
    prior_entitled: Decimal,
    mode: str,
) -> Decimal:
    """Entitled amount of the next period; ``accrued_count`` is the number already posted."""
    n = accrued_count + 1
    return max(
        Decimal("0.00"), round_days(annual_days * n / periods_per_year, mode) - prior_entitled
    )


def apply_cap(entitled: Decimal, max_balance: Decimal | None, posted_balance: Decimal) -> Decimal:
    if max_balance is None:
        return entitled
    return min(entitled, max(Decimal("0.00"), max_balance - posted_balance))


def upfront_entitlement(  # noqa: PLR0917 - mirrors the six §6.6 inputs
    annual_days: Decimal,
    proration: str,
    joining_date: date,
    year_start: date,
    cutoff_day: int,
    mode: str,
) -> Decimal:
    if proration == "NONE" or joining_date <= year_start:
        return annual_days
    months = (year_start.year * 12 + year_start.month + 12) - (
        joining_date.year * 12 + joining_date.month
    )
    if joining_date.day > cutoff_day:
        months -= 1
    return round_days(annual_days * months / 12, mode)


# ----------------------------------------------------------------------- duration (§6.9)

Slot = tuple[date, Half]


def slots_between(start: date, start_half: Half, end: date, end_half: Half) -> Iterator[Slot]:
    day = start
    while day <= end:
        for half in (Half.FIRST_HALF, Half.SECOND_HALF):
            if (day, half) < (start, start_half) or (day, half) > (end, end_half):
                continue
            yield day, half
        day += timedelta(days=1)


def counted(day_type: str, *, count_weekly_offs: bool, count_holidays: bool) -> bool:
    if day_type == "WORKING_DAY":
        return True
    return count_weekly_offs if day_type == "WEEKLY_OFF" else count_holidays


@dataclass(frozen=True, slots=True)
class SlotResult:
    slot_date: date
    half: Half
    day_type: str
    counted: bool


def evaluate_slots(
    start: date,
    start_half: Half,
    end: date,
    end_half: Half,
    day_type_of: Callable[[date], str],
    *,
    count_weekly_offs: bool,
    count_holidays: bool,
) -> tuple[list[SlotResult], Decimal]:
    cache: dict[date, str] = {}
    results = []
    for day, half in slots_between(start, start_half, end, end_half):
        kind = cache.setdefault(day, day_type_of(day))
        results.append(
            SlotResult(
                day,
                half,
                kind,
                counted(kind, count_weekly_offs=count_weekly_offs, count_holidays=count_holidays),
            )
        )
    return results, HALF * sum(1 for slot in results if slot.counted)
