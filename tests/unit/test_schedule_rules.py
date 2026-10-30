"""§5.2 weekly-off matching, including the spec's own "Sundays + 2nd and 4th Saturdays"."""

from datetime import date

from app.services.schedule_rules import is_weekly_off, occurrences_of, validate_pattern_rules

SUN_2SAT_4SAT = [(7, "ALL"), (6, "SECOND"), (6, "FOURTH")]


def test_all_sundays_and_second_fourth_saturdays() -> None:
    assert is_weekly_off(date(2026, 11, 1), SUN_2SAT_4SAT)  # Sunday
    assert not is_weekly_off(date(2026, 11, 7), SUN_2SAT_4SAT)  # 1st Saturday
    assert is_weekly_off(date(2026, 11, 14), SUN_2SAT_4SAT)  # 2nd Saturday
    assert not is_weekly_off(date(2026, 11, 21), SUN_2SAT_4SAT)  # 3rd Saturday
    assert is_weekly_off(date(2026, 11, 28), SUN_2SAT_4SAT)  # 4th Saturday
    assert not is_weekly_off(date(2026, 11, 16), SUN_2SAT_4SAT)  # Monday


def test_last_and_fifth_occurrence() -> None:
    # 30 Nov 2026 is a Monday: both the fifth and the last Monday of November.
    assert {o.value for o in occurrences_of(date(2026, 11, 30))} >= {"FIFTH", "LAST", "ALL"}
    assert is_weekly_off(date(2026, 11, 30), [(1, "LAST")])
    assert not is_weekly_off(date(2026, 11, 23), [(1, "LAST")])  # four Mondays after the 2nd


def test_empty_pattern_means_no_weekly_offs() -> None:
    assert not is_weekly_off(date(2026, 11, 1), [])


def test_validation() -> None:
    assert validate_pattern_rules(SUN_2SAT_4SAT) is None
    assert validate_pattern_rules([(7, "ALL"), (7, "FIRST")]) is not None
    assert validate_pattern_rules([(8, "ALL")]) is not None
    assert validate_pattern_rules([(6, "SECOND"), (6, "SECOND")]) is not None
