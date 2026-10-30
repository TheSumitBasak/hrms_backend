"""Pure attendance rules (addendum 5.8, 5.10, 5.11). No database access; ``now`` is a parameter.

Every duration is computed on UTC instants truncated to the whole minute, so a daylight-saving
transition in the policy timezone cannot distort worked time (5.10 "Minute arithmetic").
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

FIRST_HALF = "FIRST_HALF"
SECOND_HALF = "SECOND_HALF"
EVALUATION_VERSION = 1


def minute(instant: datetime) -> int:
    """5.10: ``m(t) = floor(epoch_seconds / 60)``."""
    return int(instant.timestamp() // 60)


def at_local(day: date, clock: time, tz: ZoneInfo) -> datetime:
    return datetime.combine(day, clock, tzinfo=tz).astimezone(UTC)


# --------------------------------------------------------------------------- business date (5.8)


@dataclass(frozen=True, slots=True)
class DayWindow:
    """How a punch is matched to business date ``d`` (shift window, or flexible day)."""

    tz: ZoneInfo
    flexible_day_start: time
    shift_start: time | None = None
    shift_end: time | None = None
    window_before: int = 0
    window_after: int = 0

    @property
    def is_flexible(self) -> bool:
        return self.shift_start is None

    def shift_bounds(self, day: date) -> tuple[datetime, datetime]:
        """``(start(d), end(d))`` in UTC; an overnight shift ends on the next calendar date."""
        assert self.shift_start is not None and self.shift_end is not None  # noqa: S101
        end_day = day + timedelta(days=1) if self.shift_end < self.shift_start else day
        return at_local(day, self.shift_start, self.tz), at_local(end_day, self.shift_end, self.tz)

    def bounds(self, day: date) -> tuple[datetime, datetime]:
        if self.is_flexible:
            return (
                at_local(day, self.flexible_day_start, self.tz),
                at_local(day + timedelta(days=1), self.flexible_day_start, self.tz),
            )
        start, end = self.shift_bounds(day)
        return (
            start - timedelta(minutes=self.window_before),
            end + timedelta(minutes=self.window_after),
        )


class NoApplicablePolicy(Exception):  # noqa: N818 - domain signal mapped to an EmsError by callers
    """Neither d0 nor d0 - 1 has an attendance policy (5.8 step 1)."""


@dataclass(frozen=True, slots=True)
class BusinessDate:
    business_date: date
    in_window: bool  # False -> OUT_OF_WINDOW, attached to d0


def assign_business_date(
    instant: datetime,
    organization_tz: ZoneInfo,
    window_for: Callable[[date], DayWindow | None],
) -> BusinessDate:
    d0 = instant.astimezone(organization_tz).date()
    anchor = window_for(d0) or window_for(d0 - timedelta(days=1))
    if anchor is None:
        raise NoApplicablePolicy
    d0 = instant.astimezone(anchor.tz).date()
    for candidate in (d0 - timedelta(days=1), d0):
        window = window_for(candidate)
        if window is None:
            continue
        start, end = window.bounds(candidate)
        if start <= instant < end:
            return BusinessDate(candidate, True)
    return BusinessDate(d0, False)


# Evaluation, spec section 5.10.


@dataclass(frozen=True, slots=True)
class PunchIn:
    id: uuid.UUID
    at: datetime
    type: str  # IN | OUT
    work_arrangement: str | None = None


@dataclass(frozen=True, slots=True)
class ShiftTimes:
    scheduled_start: datetime
    scheduled_end: datetime
    duration_minutes: int
    break_minutes: int
    full_day_min: int
    half_day_min: int
    window_after_minutes: int

    @property
    def midpoint(self) -> datetime:
        return self.scheduled_start + timedelta(minutes=self.duration_minutes // 2)


@dataclass(frozen=True, slots=True)
class PolicyTimes:
    evaluation_mode: str  # FIRST_IN_LAST_OUT | PAIRED_PUNCHES
    break_mode: str  # NONE | FIXED_FROM_SHIFT
    late_grace: int = 0
    early_grace: int = 0
    flexible_full_day_min: int = 480
    flexible_half_day_min: int = 240


@dataclass(frozen=True, slots=True)
class FixedHalf:
    status: str
    source: str  # LEAVE | OVERRIDE | ON_DUTY
    leave_application_id: uuid.UUID | None = None


@dataclass(slots=True)
class Result:
    first_in: datetime | None = None
    last_out: datetime | None = None
    gross_minutes: int = 0
    break_deducted: int = 0
    worked_minutes: int | None = None
    late_by: int | None = None
    early_by: int | None = None
    is_late: bool = False
    is_early: bool = False
    has_missing_punch: bool = False
    halves: dict[str, str | None] = field(default_factory=dict)
    sources: dict[str, str] = field(default_factory=dict)
    leave_ids: dict[str, uuid.UUID | None] = field(default_factory=dict)
    day_status: str = "INCOMPLETE"
    work_arrangement: str = "UNSPECIFIED"


def _intervals(
    punches: Sequence[PunchIn], mode: str
) -> tuple[list[tuple[datetime, datetime]], bool]:
    ordered = sorted(punches, key=lambda p: (p.at, str(p.id)))
    if mode == "FIRST_IN_LAST_OUT":
        ins = [p for p in ordered if p.type == "IN"]
        if not ins:
            return [], bool(ordered)
        first = ins[0]
        outs = [p for p in ordered if p.type == "OUT" and minute(p.at) > minute(first.at)]
        if not outs:
            return [], True
        return [(first.at, outs[-1].at)], False
    missing = False
    open_in: PunchIn | None = None
    found: list[tuple[datetime, datetime]] = []
    for punch in ordered:
        if punch.type == "IN":
            missing = missing or open_in is not None
            open_in = punch
        elif open_in is None:
            missing = True
        else:
            if minute(punch.at) > minute(open_in.at):
                found.append((open_in.at, punch.at))
            open_in = None
    return found, missing or open_in is not None


def day_status(first: str | None, second: str | None) -> str:
    """5.11: any undetermined half -> INCOMPLETE; equal halves -> that value; else SPLIT."""
    if first is None or second is None:
        return "INCOMPLETE"
    return first if first == second else "SPLIT"


def evaluate(  # noqa: PLR0912, PLR0915 - the numbered 5.10 steps, in order
    punches: Sequence[PunchIn],
    *,
    shift: ShiftTimes | None,
    policy: PolicyTimes,
    day_type: str,
    fixed: dict[str, FixedHalf],
    window_closed: bool,
) -> Result:
    """Deterministic evaluation of one record from its VALID punches and fixed halves."""
    result = Result()
    intervals, missing = _intervals(punches, policy.evaluation_mode)
    result.has_missing_punch = missing
    if intervals:
        result.first_in = min(start for start, _ in intervals)
        result.last_out = max(end for _, end in intervals)
    result.gross_minutes = sum(minute(end) - minute(start) for start, end in intervals)
    if policy.break_mode == "FIXED_FROM_SHIFT" and shift is not None:
        result.break_deducted = min(shift.break_minutes, result.gross_minutes)
    worked = result.gross_minutes - result.break_deducted
    result.worked_minutes = (
        None if (policy.evaluation_mode == "FIRST_IN_LAST_OUT" and missing) else worked
    )

    fixed_status = {half: fixed[half].status for half in fixed}
    # Step 6: fixed halves first, then the day type fills every other half.
    for half in (FIRST_HALF, SECOND_HALF):
        if half in fixed:
            result.halves[half] = fixed[half].status
            result.sources[half] = fixed[half].source
            result.leave_ids[half] = fixed[half].leave_application_id
        else:
            result.sources[half] = "EVALUATED"
            result.leave_ids[half] = None
            result.halves[half] = day_type if day_type != "WORKING_DAY" else None

    # Step 5: late / early (shift employees on working days only).
    both_on_leave = all(fixed_status.get(h) == "ON_LEAVE" for h in (FIRST_HALF, SECOND_HALF))
    if shift is not None and day_type == "WORKING_DAY" and not both_on_leave:
        ref_start = (
            shift.midpoint if fixed_status.get(FIRST_HALF) == "ON_LEAVE" else shift.scheduled_start
        )
        ref_end = (
            shift.midpoint if fixed_status.get(SECOND_HALF) == "ON_LEAVE" else shift.scheduled_end
        )
        if result.first_in is not None:
            result.late_by = max(0, minute(result.first_in) - minute(ref_start))
            result.is_late = result.late_by > policy.late_grace
        if result.last_out is not None:
            result.early_by = max(0, minute(ref_end) - minute(result.last_out))
            result.is_early = result.early_by > policy.early_grace

    free = [h for h in (FIRST_HALF, SECOND_HALF) if h not in fixed and day_type == "WORKING_DAY"]
    if free and window_closed and not result.has_missing_punch:
        full_min = shift.full_day_min if shift else policy.flexible_full_day_min
        half_min = shift.half_day_min if shift else policy.flexible_half_day_min
        valid = bool(punches)
        w = result.worked_minutes or 0
        if not valid:
            for h in free:
                result.halves[h] = "ABSENT"
        elif len(free) == 2:
            if w >= full_min:
                result.halves[FIRST_HALF] = result.halves[SECOND_HALF] = "PRESENT"
            elif w >= half_min:
                first_present = (
                    shift is None
                    or result.first_in is None
                    or minute(result.first_in) < minute(shift.midpoint)
                )
                result.halves[FIRST_HALF] = "PRESENT" if first_present else "ABSENT"
                result.halves[SECOND_HALF] = "ABSENT" if first_present else "PRESENT"
            else:
                result.halves[FIRST_HALF] = result.halves[SECOND_HALF] = "ABSENT"
        else:
            result.halves[free[0]] = "PRESENT" if w >= half_min else "ABSENT"

    result.day_status = day_status(result.halves[FIRST_HALF], result.halves[SECOND_HALF])
    result.work_arrangement = _work_arrangement(punches, fixed)
    return result


def _work_arrangement(punches: Sequence[PunchIn], fixed: dict[str, FixedHalf]) -> str:
    if any(f.source == "ON_DUTY" for f in fixed.values()):
        return "ON_DUTY"
    ordered = sorted(punches, key=lambda p: (p.at, str(p.id)))
    first_in = next((p for p in ordered if p.type == "IN"), None)
    if first_in is not None and first_in.work_arrangement == "REMOTE":
        return "REMOTE"
    if any(p.work_arrangement == "OFFICE" for p in ordered):
        return "OFFICE"
    return "UNSPECIFIED"


def window_closed(
    now: datetime,
    business_date: date,
    *,
    shift_end: datetime | None,
    window_after_minutes: int,
    tz: ZoneInfo,
    flexible_day_start: time,
) -> bool:
    """5.10 step 6: when free halves may be evaluated."""
    if shift_end is not None:
        return now >= shift_end + timedelta(minutes=window_after_minutes)
    return now >= at_local(business_date + timedelta(days=1), flexible_day_start, tz)
