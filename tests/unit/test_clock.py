"""Interval, date and financial-year arithmetic (§1.4, §18.1, §19.1)."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from app.core.clock import (
    FixedClock,
    SystemClock,
    YearFormat,
    age_on,
    exclusive_end_of_inclusive_day,
    financial_year_start_year,
    inclusive_day_of_exclusive_end,
    interval_contains,
    interval_is_valid,
    intervals_overlap,
    render_year_token,
    sequence_scope_key,
    to_utc,
    today_in,
    utc_now,
)

pytestmark = pytest.mark.unit


class TestTimezoneHandling:
    def test_utc_now_is_timezone_aware(self) -> None:
        assert utc_now().tzinfo is not None

    def test_naive_datetime_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="naive datetime"):
            to_utc(datetime(2026, 10, 28, 12, 0))  # noqa: DTZ001 — deliberately naive

    def test_today_uses_organization_timezone_not_server_timezone(self) -> None:
        """§1.4: "today" is the current date where the organization is.

        19:00 UTC on the 28th is already 00:30 on the 29th in Asia/Kolkata (UTC+5:30).
        """
        evening_utc = datetime(2026, 10, 28, 19, 0, tzinfo=UTC)
        assert today_in("Asia/Kolkata", now=evening_utc) == date(2026, 10, 29)
        assert today_in("UTC", now=evening_utc) == date(2026, 10, 28)

    def test_unknown_timezone_falls_back_to_default(self) -> None:
        evening_utc = datetime(2026, 10, 28, 19, 0, tzinfo=UTC)
        assert today_in("Mars/Olympus_Mons", now=evening_utc) == date(2026, 10, 29)

    def test_fixed_clock_is_frozen(self) -> None:
        clock = FixedClock(datetime(2026, 10, 28, 6, 44, tzinfo=UTC))
        assert clock.now() == datetime(2026, 10, 28, 6, 44, tzinfo=UTC)
        assert clock.today_for("Asia/Kolkata") == date(2026, 10, 28)

    def test_system_clock_reports_a_date(self) -> None:
        assert isinstance(SystemClock().today_for("Asia/Kolkata"), date)


class TestHalfOpenIntervals:
    """§18.1: intervals are ``[effective_from, effective_to)``."""

    def test_start_is_inclusive(self) -> None:
        assert interval_contains(date(2026, 4, 1), date(2027, 4, 1), date(2026, 4, 1))

    def test_end_is_exclusive(self) -> None:
        assert not interval_contains(date(2026, 4, 1), date(2027, 4, 1), date(2027, 4, 1))

    def test_null_end_is_open_ended(self) -> None:
        assert interval_contains(date(2026, 4, 1), None, date(2099, 1, 1))

    def test_before_start_is_excluded(self) -> None:
        assert not interval_contains(date(2026, 4, 1), None, date(2026, 3, 31))

    def test_adjacent_intervals_do_not_overlap(self) -> None:
        """The §18.1 worked statement: A [2026-04-01, 2027-01-01) and B [2027-01-01, NULL)."""
        assert not intervals_overlap(date(2026, 4, 1), date(2027, 1, 1), date(2027, 1, 1), None)

    def test_identical_start_overlaps(self) -> None:
        """§18.3 step 3 / E-16: two revisions with the same effective_from conflict."""
        assert intervals_overlap(date(2027, 1, 1), None, date(2027, 1, 1), None)

    def test_contained_interval_overlaps(self) -> None:
        assert intervals_overlap(
            date(2026, 1, 1), date(2028, 1, 1), date(2027, 1, 1), date(2027, 6, 1)
        )

    def test_two_open_ended_intervals_always_overlap(self) -> None:
        assert intervals_overlap(date(2026, 1, 1), None, date(2030, 1, 1), None)

    def test_overlap_is_symmetric(self) -> None:
        first = (date(2026, 1, 1), date(2027, 1, 1))
        second = (date(2026, 6, 1), None)
        assert intervals_overlap(*first, *second) == intervals_overlap(*second, *first)

    @pytest.mark.parametrize(
        ("start", "end", "valid"),
        [
            (date(2026, 1, 1), None, True),
            (date(2026, 1, 1), date(2026, 1, 2), True),
            (date(2026, 1, 1), date(2026, 1, 1), False),
            (date(2026, 1, 2), date(2026, 1, 1), False),
        ],
    )
    def test_inv_2_end_must_follow_start(self, start: date, end: date | None, valid: bool) -> None:
        assert interval_is_valid(start, end) is valid


class TestInclusiveEmploymentEndDate:
    """§1.4 exception: employment_end_date is the inclusive last day of employment."""

    def test_exclusive_boundary_is_the_day_after(self) -> None:
        assert exclusive_end_of_inclusive_day(date(2027, 3, 31)) == date(2027, 4, 1)

    def test_none_stays_none(self) -> None:
        assert exclusive_end_of_inclusive_day(None) is None
        assert inclusive_day_of_exclusive_end(None) is None

    def test_round_trip(self) -> None:
        last_day = date(2027, 3, 31)
        assert inclusive_day_of_exclusive_end(exclusive_end_of_inclusive_day(last_day)) == last_day

    def test_last_day_of_employment_is_covered_by_the_interval(self) -> None:
        """INV-4: an assignment ending at E+1 still covers E itself."""
        end_exclusive = exclusive_end_of_inclusive_day(date(2027, 3, 31))
        assert interval_contains(date(2026, 4, 1), end_exclusive, date(2027, 3, 31))


class TestAge:
    def test_age_on_birthday(self) -> None:
        assert age_on(date(2008, 3, 2), date(2026, 3, 2)) == 18

    def test_age_day_before_birthday(self) -> None:
        assert age_on(date(2008, 3, 2), date(2026, 3, 1)) == 17

    def test_leap_day_birth(self) -> None:
        assert age_on(date(2008, 2, 29), date(2026, 2, 28)) == 17
        assert age_on(date(2008, 2, 29), date(2026, 3, 1)) == 18


class TestFinancialYear:
    @pytest.mark.parametrize(
        ("moment", "expected"),
        [
            (date(2026, 3, 31), 2025),
            (date(2026, 4, 1), 2026),
            (date(2026, 12, 31), 2026),
            (date(2027, 1, 1), 2026),
        ],
    )
    def test_april_start(self, moment: date, expected: int) -> None:
        assert financial_year_start_year(moment, 4) == expected

    def test_january_start_matches_calendar_year(self) -> None:
        assert financial_year_start_year(date(2026, 7, 1), 1) == 2026

    def test_invalid_start_month_rejected(self) -> None:
        with pytest.raises(ValueError, match="between 1 and 12"):
            financial_year_start_year(date(2026, 7, 1), 13)

    @pytest.mark.parametrize(
        ("year_format", "expected"),
        [
            (YearFormat.YYYY, "2026"),
            (YearFormat.YY, "26"),
            (YearFormat.FY_SHORT, "2627"),
            (YearFormat.FY_LONG, "2026-27"),
        ],
    )
    def test_year_token_examples_from_spec(self, year_format: YearFormat, expected: str) -> None:
        """§19.1/§19.2: FY_SHORT renders 2026-27 as 2627, FY_LONG as 2026-27."""
        assert render_year_token(date(2026, 11, 2), year_format) == expected

    def test_financial_year_token_before_april_uses_previous_year(self) -> None:
        assert render_year_token(date(2026, 3, 31), YearFormat.FY_SHORT) == "2526"
        assert render_year_token(date(2026, 3, 31), YearFormat.FY_LONG) == "2025-26"

    def test_century_rollover(self) -> None:
        assert render_year_token(date(2099, 6, 1), YearFormat.FY_SHORT) == "9900"

    def test_is_financial_year_flag(self) -> None:
        assert YearFormat.FY_SHORT.is_financial_year
        assert YearFormat.FY_LONG.is_financial_year
        assert not YearFormat.YYYY.is_financial_year


class TestSequenceScopeKey:
    """§19.6 step 2."""

    def test_never_uses_a_single_scope(self) -> None:
        assert sequence_scope_key(date(2026, 11, 2), "NEVER") == "ALL"

    def test_calendar_year(self) -> None:
        assert sequence_scope_key(date(2026, 11, 2), "CALENDAR_YEAR") == "CY2026"

    def test_financial_year_uses_the_starting_year(self) -> None:
        assert sequence_scope_key(date(2026, 11, 2), "FINANCIAL_YEAR") == "FY2026"
        assert sequence_scope_key(date(2026, 3, 31), "FINANCIAL_YEAR") == "FY2025"

    def test_scope_key_fits_the_column(self) -> None:
        """§19.9 types scope_key as VARCHAR(20)."""
        for policy in ("NEVER", "CALENDAR_YEAR", "FINANCIAL_YEAR"):
            assert len(sequence_scope_key(date(2026, 11, 2), policy)) <= 20

    def test_unknown_policy_rejected(self) -> None:
        with pytest.raises(ValueError, match="unknown sequence reset policy"):
            sequence_scope_key(date(2026, 11, 2), "WEEKLY")
