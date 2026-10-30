"""Pure work-schedule rules (addendum §5.2, §5.6). No database access."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, timedelta

from app.models.attendance import WeeklyOffOccurrence

_ORDINALS = {
    1: WeeklyOffOccurrence.FIRST,
    2: WeeklyOffOccurrence.SECOND,
    3: WeeklyOffOccurrence.THIRD,
    4: WeeklyOffOccurrence.FOURTH,
    5: WeeklyOffOccurrence.FIFTH,
}


def occurrences_of(day: date) -> frozenset[WeeklyOffOccurrence]:
    """Every occurrence label the date answers to: ALL, its ordinal, and LAST if it is last."""
    labels = {WeeklyOffOccurrence.ALL, _ORDINALS[(day.day - 1) // 7 + 1]}
    if (day + timedelta(days=7)).month != day.month:
        labels.add(WeeklyOffOccurrence.LAST)
    return frozenset(labels)


def is_weekly_off(day: date, rules: Iterable[tuple[int, str]]) -> bool:
    """§5.2: ``rules`` are ``(ISO weekday, occurrence)`` rows; an empty pattern is never off."""
    labels = {label.value for label in occurrences_of(day)}
    weekday = day.isoweekday()
    return any(dow == weekday and occurrence in labels for dow, occurrence in rules)


def validate_pattern_rules(rules: Iterable[tuple[int, str]]) -> str | None:
    """Return a problem description, or ``None``. ``ALL`` for a day excludes other rows (§5.2)."""
    seen: dict[int, set[str]] = {}
    for dow, occurrence in rules:
        if not 1 <= dow <= 7:
            return f"day_of_week {dow} is outside 1-7"
        if occurrence not in {member.value for member in WeeklyOffOccurrence}:
            return f"unknown occurrence {occurrence}"
        if occurrence in seen.setdefault(dow, set()):
            return f"duplicate rule for day {dow} occurrence {occurrence}"
        seen[dow].add(occurrence)
    for dow, found in seen.items():
        if "ALL" in found and len(found) > 1:
            return f"day {dow} has ALL together with other occurrences"
    return None
