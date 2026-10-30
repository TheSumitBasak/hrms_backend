"""Pure employee-code rendering (§19.1, §19.2)."""

from __future__ import annotations

from datetime import date

import pytest

from app.services.employee_codes import PatternError, parse_pattern, render_code, validate_pattern


def _render(pattern: str, sequence: int = 1, **overrides: object) -> str:
    values = {
        "pattern": pattern,
        "organization_code": "ABC",
        "department_code": "IT",
        "prefix": "EMP",
        "suffix": "C",
        "separator": "-",
        "year_format": "FY_SHORT",
        "financial_year_start_month": 4,
        "basis_date": date(2027, 3, 31),
        "sequence": sequence,
        "padding": 4,
    }
    values.update(overrides)
    return render_code(**values)  # type: ignore[arg-type]


class TestRendering:
    def test_examples_from_the_spec(self) -> None:
        assert _render("{ORG}{SEP}{SEQ}") == "ABC-0001"
        assert _render("{ORG}{SEP}{DEPT}{SEP}{SEQ}") == "ABC-IT-0001"
        assert (
            _render(
                "{DEPT}{SEP}{YEAR}{SEP}{SEQ}",
                year_format="YYYY",
                padding=5,
                basis_date=date(2027, 1, 1),
            )
            == "IT-2027-00001"
        )
        assert (
            _render(
                "{ORG}{SEP}{DEPT}{SEP}{YEAR}{SEP}{SEQ}",
                basis_date=date(2026, 5, 1),
            )
            == "ABC-IT-2627-0001"
        )
        assert _render("{PREFIX}{SEQ}{SUFFIX}", padding=6) == "EMP000001C"

    def test_length_and_pattern_are_enforced(self) -> None:
        with pytest.raises(PatternError):
            _render("{ORG}{SEQ}", padding=40)
        with pytest.raises(PatternError):
            _render("{DEPT}{SEQ}", department_code=None)

    def test_parser_rejects_unknown_tokens_and_lowercase_literals(self) -> None:
        with pytest.raises(PatternError):
            parse_pattern("{NOPE}{SEQ}")
        with pytest.raises(PatternError):
            parse_pattern("abc{SEQ}")


class TestPatternValidation:
    def test_seq_exactly_once(self) -> None:
        assert (
            validate_pattern(
                "{ORG}{SEQ}", prefix=None, suffix=None, separator="-", year_format=None
            )
            == []
        )

    def test_missing_seq_and_year_mismatch(self) -> None:
        errors = validate_pattern(
            "{ORG}", prefix=None, suffix=None, separator="-", year_format=None
        )
        assert any(error.code == "SEQ_COUNT" for error in errors)

        errors = validate_pattern(
            "{YEAR}{SEQ}", prefix=None, suffix=None, separator="-", year_format=None
        )
        assert any(error.code == "YEAR_FORMAT" for error in errors)

    def test_sep_requires_separator(self) -> None:
        errors = validate_pattern(
            "{ORG}{SEP}{SEQ}", prefix=None, suffix=None, separator=None, year_format=None
        )
        assert any(error.field == "separator" for error in errors)
