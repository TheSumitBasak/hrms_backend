"""Business-date assignment and record evaluation against the addendum's examples (5.8, 5.10)."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

import pytest

from app.services.attendance_rules import (
    DayWindow,
    FixedHalf,
    NoApplicablePolicy,
    PolicyTimes,
    PunchIn,
    ShiftTimes,
    assign_business_date,
    at_local,
    day_status,
    evaluate,
    window_closed,
)

IST = ZoneInfo("Asia/Kolkata")
NEW_YORK = ZoneInfo("America/New_York")
NIGHT = DayWindow(
    tz=IST,
    flexible_day_start=time(0, 0),
    shift_start=time(22, 0),
    shift_end=time(6, 0),
    window_before=120,
    window_after=240,
)
POLICY = PolicyTimes("FIRST_IN_LAST_OUT", "FIXED_FROM_SHIFT", late_grace=10, early_grace=10)


def _ist(day: int, hour: int, minute: int, month: int = 11) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=IST).astimezone(UTC)


def _punch(kind: str, at: datetime, arrangement: str | None = None) -> PunchIn:
    return PunchIn(uuid.uuid4(), at, kind, arrangement)


def _night_shift(business_date: date = date(2026, 11, 2)) -> ShiftTimes:
    start, end = NIGHT.shift_bounds(business_date)
    return ShiftTimes(start, end, 480, 30, 450, 225, 240)


class TestBusinessDate:
    def test_overnight_example_from_the_spec(self) -> None:
        window = lambda _d: NIGHT  # noqa: E731
        assert assign_business_date(_ist(2, 21, 55), IST, window).business_date == date(2026, 11, 2)
        out = assign_business_date(_ist(3, 6, 10), IST, window)
        assert (out.business_date, out.in_window) == (date(2026, 11, 2), True)

    def test_outside_every_window_is_out_of_window_on_d0(self) -> None:
        result = assign_business_date(_ist(3, 14, 0), IST, lambda _d: NIGHT)
        assert (result.business_date, result.in_window) == (date(2026, 11, 3), False)

    def test_flexible_day_boundary(self) -> None:
        flexible = DayWindow(tz=IST, flexible_day_start=time(4, 0))
        # 02:00 belongs to the previous business day when the day starts at 04:00.
        assert assign_business_date(_ist(3, 2, 0), IST, lambda _d: flexible).business_date == date(
            2026, 11, 2
        )
        assert assign_business_date(_ist(3, 4, 0), IST, lambda _d: flexible).business_date == date(
            2026, 11, 3
        )

    def test_no_policy_on_d0_or_the_day_before_is_an_error(self) -> None:
        with pytest.raises(NoApplicablePolicy):
            assign_business_date(_ist(3, 9, 0), IST, lambda _d: None)

    def test_policy_timezone_decides_the_calendar_date(self) -> None:
        flexible = DayWindow(tz=NEW_YORK, flexible_day_start=time(0, 0))
        instant = datetime(2026, 11, 3, 2, 0, tzinfo=UTC)  # 21:00 on 11-02 in New York
        assert assign_business_date(instant, IST, lambda _d: flexible).business_date == date(
            2026, 11, 2
        )

    def test_overnight_shift_across_a_dst_change(self) -> None:
        """NFR-31: 22:00-06:00 New York, spring-forward on 2026-03-08."""
        window = DayWindow(
            tz=NEW_YORK,
            flexible_day_start=time(0, 0),
            shift_start=time(22, 0),
            shift_end=time(6, 0),
            window_before=120,
            window_after=240,
        )
        punch_in = datetime(2026, 3, 7, 21, 55, tzinfo=NEW_YORK)
        punch_out = datetime(2026, 3, 8, 6, 10, tzinfo=NEW_YORK)
        for instant in (punch_in, punch_out):
            assert assign_business_date(instant, NEW_YORK, lambda _d: window).business_date == date(
                2026, 3, 7
            )
        start, end = window.shift_bounds(date(2026, 3, 7))
        assert int((end - start).total_seconds() // 60) == 420  # 7 real hours, not 8
        shift = ShiftTimes(start, end, 420, 0, 400, 200, 240)
        result = evaluate(
            [_punch("IN", punch_in), _punch("OUT", punch_out)],
            shift=shift,
            policy=PolicyTimes("FIRST_IN_LAST_OUT", "NONE"),
            day_type="WORKING_DAY",
            fixed={},
            window_closed=True,
        )
        assert result.gross_minutes == 435  # UTC arithmetic: 21:55 EST -> 06:10 EDT


class TestEvaluation:
    def _run(self, *punches: PunchIn, **overrides: object) -> object:
        kwargs: dict[str, object] = {
            "shift": _night_shift(),
            "policy": POLICY,
            "day_type": "WORKING_DAY",
            "fixed": {},
            "window_closed": True,
        }
        kwargs.update(overrides)
        return evaluate(list(punches), **kwargs)  # type: ignore[arg-type]

    def test_full_night_shift_worked_example(self) -> None:
        r = self._run(_punch("IN", _ist(2, 21, 55)), _punch("OUT", _ist(3, 6, 10)))
        assert (r.gross_minutes, r.break_deducted, r.worked_minutes) == (495, 30, 465)
        assert (r.halves["FIRST_HALF"], r.halves["SECOND_HALF"]) == ("PRESENT", "PRESENT")
        assert (r.late_by, r.early_by, r.is_late, r.is_early) == (0, 0, False, False)
        assert r.day_status == "PRESENT"

    def test_early_exit_is_a_split_day_with_the_first_half_present(self) -> None:
        r = self._run(_punch("IN", _ist(2, 21, 55)), _punch("OUT", _ist(3, 2, 30)))
        assert (r.gross_minutes, r.worked_minutes) == (275, 245)
        assert (r.halves["FIRST_HALF"], r.halves["SECOND_HALF"]) == ("PRESENT", "ABSENT")
        assert r.day_status == "SPLIT"
        assert (r.early_by, r.is_early) == (210, True)

    def test_grace_affects_flags_not_minutes(self) -> None:
        r = self._run(_punch("IN", _ist(2, 22, 8)), _punch("OUT", _ist(3, 6, 0)))
        assert (r.late_by, r.is_late) == (8, False)
        r = self._run(_punch("IN", _ist(2, 22, 12)), _punch("OUT", _ist(3, 6, 0)))
        assert (r.late_by, r.is_late) == (12, True)

    def test_missing_out_is_incomplete_not_absent(self) -> None:
        r = self._run(_punch("IN", _ist(2, 22, 0)))
        assert r.has_missing_punch and r.worked_minutes is None
        assert r.halves["FIRST_HALF"] is None and r.day_status == "INCOMPLETE"

    def test_no_punches_after_the_window_is_absent(self) -> None:
        assert self._run().day_status == "ABSENT"

    def test_free_halves_stay_undetermined_until_the_window_closes(self) -> None:
        r = self._run(
            _punch("IN", _ist(2, 21, 55)), _punch("OUT", _ist(3, 6, 10)), window_closed=False
        )
        assert r.day_status == "INCOMPLETE" and r.halves["FIRST_HALF"] is None

    def test_weekly_off_and_holiday_fill_the_halves(self) -> None:
        r = self._run(day_type="WEEKLY_OFF", window_closed=False)
        assert (r.day_status, r.late_by) == ("WEEKLY_OFF", None)

    def test_leave_half_is_fixed_and_the_free_half_uses_the_half_day_threshold(self) -> None:
        leave = FixedHalf("ON_LEAVE", "LEAVE", uuid.uuid4())
        r = self._run(
            _punch("IN", _ist(3, 1, 50)),
            _punch("OUT", _ist(3, 6, 10)),
            fixed={"FIRST_HALF": leave},
        )
        assert r.halves["FIRST_HALF"] == "ON_LEAVE" and r.sources["FIRST_HALF"] == "LEAVE"
        assert r.halves["SECOND_HALF"] == "PRESENT"  # 260 gross - 30 break = 230 >= half-day 225
        assert r.day_status == "SPLIT"

    def test_late_is_measured_from_the_midpoint_when_the_first_half_is_on_leave(self) -> None:
        leave = FixedHalf("ON_LEAVE", "LEAVE", uuid.uuid4())
        r = self._run(
            _punch("IN", _ist(3, 2, 20)), _punch("OUT", _ist(3, 6, 0)), fixed={"FIRST_HALF": leave}
        )
        assert r.late_by == 20  # midpoint is 02:00

    def test_both_halves_on_leave_skip_late_and_early(self) -> None:
        leave = FixedHalf("ON_LEAVE", "LEAVE", uuid.uuid4())
        r = self._run(
            _punch("IN", _ist(2, 23, 0)),
            fixed={"FIRST_HALF": leave, "SECOND_HALF": leave},
        )
        assert (r.late_by, r.early_by, r.day_status) == (None, None, "ON_LEAVE")

    def test_paired_punches_treat_gaps_as_breaks(self) -> None:
        policy = PolicyTimes("PAIRED_PUNCHES", "NONE")
        r = self._run(
            _punch("IN", _ist(2, 22, 0)),
            _punch("OUT", _ist(3, 1, 0)),
            _punch("IN", _ist(3, 2, 0)),
            _punch("OUT", _ist(3, 6, 0)),
            policy=policy,
        )
        assert (r.gross_minutes, r.has_missing_punch) == (420, False)

    def test_paired_punches_flag_an_orphan_out(self) -> None:
        policy = PolicyTimes("PAIRED_PUNCHES", "NONE")
        r = self._run(_punch("OUT", _ist(2, 22, 0)), policy=policy)
        assert r.has_missing_punch and r.day_status == "INCOMPLETE"

    def test_flexible_employee_uses_the_policy_thresholds(self) -> None:
        policy = PolicyTimes(
            "FIRST_IN_LAST_OUT", "NONE", flexible_full_day_min=480, flexible_half_day_min=240
        )
        r = self._run(
            _punch("IN", _ist(2, 9, 0)),
            _punch("OUT", _ist(2, 14, 0)),
            shift=None,
            policy=policy,
        )
        assert (r.halves["FIRST_HALF"], r.halves["SECOND_HALF"]) == ("PRESENT", "ABSENT")
        assert (r.late_by, r.early_by) == (None, None)

    def test_work_arrangement_priority(self) -> None:
        remote = self._run(_punch("IN", _ist(2, 22, 0), "REMOTE"), _punch("OUT", _ist(3, 6, 0)))
        assert remote.work_arrangement == "REMOTE"
        duty = self._run(fixed={"FIRST_HALF": FixedHalf("PRESENT", "ON_DUTY")})
        assert duty.work_arrangement == "ON_DUTY"


def test_day_status_table() -> None:
    assert day_status("PRESENT", "PRESENT") == "PRESENT"
    assert day_status("PRESENT", "ABSENT") == "SPLIT"
    assert day_status(None, "ABSENT") == "INCOMPLETE"


def test_window_closure_for_shift_and_flexible() -> None:
    start, end = NIGHT.shift_bounds(date(2026, 11, 2))
    assert not window_closed(
        end,
        date(2026, 11, 2),
        shift_end=end,
        window_after_minutes=240,
        tz=IST,
        flexible_day_start=time(0, 0),
    )
    assert window_closed(
        end.replace(hour=end.hour + 5),
        date(2026, 11, 2),
        shift_end=end,
        window_after_minutes=240,
        tz=IST,
        flexible_day_start=time(0, 0),
    )
    boundary = at_local(date(2026, 11, 3), time(0, 0), IST)
    assert window_closed(
        boundary,
        date(2026, 11, 2),
        shift_end=None,
        window_after_minutes=0,
        tz=IST,
        flexible_day_start=time(0, 0),
    )
    assert start < end
