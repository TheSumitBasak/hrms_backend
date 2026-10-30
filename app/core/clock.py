"""Time, business dates and effective-dated interval arithmetic (§1.4).

Three conventions from the spec are implemented here once, so no caller re-derives them:

1. **Timestamps are UTC.** :func:`utc_now` is the only source of "now"; naive datetimes are
   rejected by the ``DTZ`` lint rules and by :class:`app.db.types.UtcDateTime`.
2. **Business dates belong to the organization's timezone.** "Today" means the current date
   where the organization is, not where the server is — so a request at 19:00 UTC on the 1st
   is already the 2nd for an ``Asia/Kolkata`` organization.
3. **Every effective-dated interval is half-open**, ``[effective_from, effective_to)``, with
   ``effective_to = NULL`` meaning "no known end". The single exception is
   ``employment_end_date``, which is the *inclusive* last day of employment; wherever it
   bounds an interval the exclusive boundary is ``employment_end_date + 1 day``
   (:func:`exclusive_end_of_inclusive_day`).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from typing import Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_TIMEZONE = "Asia/Kolkata"
"""§7.2 default for ``organizations.timezone`` (A-02)."""

DEFAULT_FINANCIAL_YEAR_START_MONTH = 4
"""§7.2 default: the Indian financial year starts in April."""

ONE_DAY = timedelta(days=1)


def utc_now() -> datetime:
    """The current instant, timezone-aware in UTC."""
    return datetime.now(UTC)


def to_utc(moment: datetime) -> datetime:
    """Normalise an aware datetime to UTC. Naive input is a programming error."""
    if moment.tzinfo is None:
        raise ValueError("naive datetime: all timestamps must be timezone-aware (§1.4)")
    return moment.astimezone(UTC)


def resolve_timezone(name: str | None) -> ZoneInfo:
    """Load an IANA timezone, falling back to the organization default.

    An unknown name is a configuration error, but failing a read request over it would be
    worse than using the documented default, so this falls back and leaves validation of the
    ``timezone`` column to the organization service.
    """
    try:
        return ZoneInfo(name or DEFAULT_TIMEZONE)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(DEFAULT_TIMEZONE)


def today_in(timezone_name: str | None, *, now: datetime | None = None) -> date:
    """ "Today" for an organization: the current date in its timezone (§1.4)."""
    moment = now or utc_now()
    return to_utc(moment).astimezone(resolve_timezone(timezone_name)).date()


class Clock(Protocol):
    """Injectable time source.

    Services take a clock rather than calling :func:`utc_now` directly, so date-boundary
    behaviour — a revision becoming current at midnight, a correction window expiring — is
    testable without sleeping or patching module globals.
    """

    def now(self) -> datetime:
        """The current instant in UTC."""
        ...


class SystemClock:
    """Real time. The production clock."""

    __slots__ = ()

    def now(self) -> datetime:
        return utc_now()

    def today_for(self, timezone_name: str | None) -> date:
        return today_in(timezone_name, now=self.now())


class FixedClock:
    """A clock frozen at one instant, for tests and for the §18.7 worked example."""

    __slots__ = ("_moment",)

    def __init__(self, moment: datetime) -> None:
        self._moment = to_utc(moment)

    def now(self) -> datetime:
        return self._moment

    def today_for(self, timezone_name: str | None) -> date:
        return today_in(timezone_name, now=self._moment)


# -------------------------------------------------------------------------------------
# Half-open interval arithmetic (§18.1)
# -------------------------------------------------------------------------------------


def exclusive_end_of_inclusive_day(last_day: date | None) -> date | None:
    """Convert an inclusive last day to the exclusive end of a half-open interval.

    ``employment_end_date = 2027-03-31`` bounds an interval that ends ``2027-04-01``, so an
    assignment may run up to but not including that date (§1.4, INV-4).
    """
    return None if last_day is None else last_day + ONE_DAY


def inclusive_day_of_exclusive_end(end: date | None) -> date | None:
    """Inverse of :func:`exclusive_end_of_inclusive_day`, for display."""
    return None if end is None else end - ONE_DAY


def interval_contains(start: date, end: date | None, moment: date) -> bool:
    """Whether ``moment`` falls in ``[start, end)``. ``end=None`` means open-ended.

    This is the "assignment as of date D" predicate of §18.1. §18.2 requires all
    current-salary reads to use it rather than a stored status, so a scheduled revision
    becomes current at midnight even if the refresh job is late (E-12, AC-09).
    """
    if moment < start:
        return False
    return end is None or moment < end


def intervals_overlap(
    first_start: date,
    first_end: date | None,
    second_start: date,
    second_end: date | None,
) -> bool:
    """Whether two half-open intervals intersect.

    Adjacent intervals do **not** overlap: ``[2026-04-01, 2027-01-01)`` and
    ``[2027-01-01, None)`` are disjoint (§18.1). This is the INV-1 predicate.
    """
    if first_end is not None and first_end <= second_start:
        return False
    return not (second_end is not None and second_end <= first_start)


def interval_is_valid(start: date, end: date | None) -> bool:
    """INV-2: ``effective_to`` must be strictly after ``effective_from`` when present."""
    return end is None or end > start


def days_between(earlier: date, later: date) -> int:
    """Whole days from ``earlier`` to ``later``; negative when ``later`` precedes it."""
    return (later - earlier).days


def age_on(date_of_birth: date, reference: date) -> int:
    """Completed years of age on ``reference``.

    Used for the §10.4 age policy and the §13.2 ``is_minor`` derivation. Counts completed
    years, so someone born on 2008-03-02 is 17 on 2026-03-01 and 18 on 2026-03-02.
    """
    years = reference.year - date_of_birth.year
    if (reference.month, reference.day) < (date_of_birth.month, date_of_birth.day):
        years -= 1
    return years


# -------------------------------------------------------------------------------------
# Financial years and the {YEAR} token (§19.1)
# -------------------------------------------------------------------------------------


class YearFormat(StrEnum):
    """§19.1 ``year_format``."""

    YYYY = "YYYY"
    YY = "YY"
    FY_SHORT = "FY_SHORT"
    FY_LONG = "FY_LONG"

    @property
    def is_financial_year(self) -> bool:
        return self in (YearFormat.FY_SHORT, YearFormat.FY_LONG)


def financial_year_start_year(moment: date, start_month: int) -> int:
    """The calendar year in which ``moment``'s financial year began.

    With ``start_month = 4``, 2026-03-31 belongs to the financial year starting 2025 and
    2026-04-01 to the one starting 2026.
    """
    if not 1 <= start_month <= 12:
        raise ValueError("financial year start month must be between 1 and 12")
    return moment.year if moment.month >= start_month else moment.year - 1


def render_year_token(
    moment: date,
    year_format: YearFormat,
    *,
    financial_year_start_month: int = DEFAULT_FINANCIAL_YEAR_START_MONTH,
) -> str:
    """Render the ``{YEAR}`` token of an employee code (§19.1, §19.2).

    ``FY_SHORT`` renders 2026-27 as ``2627`` and ``FY_LONG`` as ``2026-27``, matching the
    §19.1 examples.
    """
    match year_format:
        case YearFormat.YYYY:
            return f"{moment.year:04d}"
        case YearFormat.YY:
            return f"{moment.year % 100:02d}"
        case YearFormat.FY_SHORT | YearFormat.FY_LONG:
            start = financial_year_start_year(moment, financial_year_start_month)
            end_short = (start + 1) % 100
            if year_format is YearFormat.FY_SHORT:
                return f"{start % 100:02d}{end_short:02d}"
            return f"{start:04d}-{end_short:02d}"


def sequence_scope_key(
    moment: date,
    reset_policy: str,
    *,
    financial_year_start_month: int = DEFAULT_FINANCIAL_YEAR_START_MONTH,
) -> str:
    """The ``scope_key`` a sequence counter is partitioned by (§19.6 step 2).

    ``NEVER`` -> ``ALL``; ``CALENDAR_YEAR`` -> ``CY2027``; ``FINANCIAL_YEAR`` -> ``FY2026``
    (the year the financial year started).
    """
    match reset_policy:
        case "NEVER":
            return "ALL"
        case "CALENDAR_YEAR":
            return f"CY{moment.year:04d}"
        case "FINANCIAL_YEAR":
            return f"FY{financial_year_start_year(moment, financial_year_start_month):04d}"
        case _:
            raise ValueError(f"unknown sequence reset policy: {reset_policy!r}")


__all__ = [
    "DEFAULT_FINANCIAL_YEAR_START_MONTH",
    "DEFAULT_TIMEZONE",
    "ONE_DAY",
    "Clock",
    "FixedClock",
    "SystemClock",
    "YearFormat",
    "age_on",
    "days_between",
    "exclusive_end_of_inclusive_day",
    "financial_year_start_year",
    "inclusive_day_of_exclusive_end",
    "interval_contains",
    "interval_is_valid",
    "intervals_overlap",
    "render_year_token",
    "resolve_timezone",
    "sequence_scope_key",
    "to_utc",
    "today_in",
    "utc_now",
]
