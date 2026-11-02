"""Pure rule-version logic: PCFG-20 resolution and slab validity (payroll addendum §4.6)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import pytest

from app.payroll.rules import resolve, schema_matches_family, slab_problems

pytestmark = pytest.mark.unit


@dataclass
class V:
    status: str
    effective_from: date
    effective_to: date | None = None


@dataclass
class S:
    slab_table_code: str
    sequence_no: int
    lower_bound: Decimal
    upper_bound: Decimal | None
    condition_code: str = ""


def slab(seq: int, low: str, high: str | None, *, table: str = "T", cond: str = "") -> S:
    return S(table, seq, Decimal(low), Decimal(high) if high else None, cond)


class TestResolve:
    versions = (
        V("PUBLISHED", date(2025, 4, 1), date(2026, 4, 1)),
        V("PUBLISHED", date(2026, 4, 1)),
        V("DRAFT", date(2027, 4, 1)),
        V("WITHDRAWN", date(2024, 4, 1), date(2025, 4, 1)),
    )

    def test_start_is_inclusive_and_end_is_exclusive(self) -> None:
        assert resolve(self.versions, date(2026, 3, 31)) is self.versions[0]
        assert resolve(self.versions, date(2026, 4, 1)) is self.versions[1]
        assert resolve(self.versions, date(2025, 4, 1)) is self.versions[0]

    def test_open_ended_version_applies_indefinitely(self) -> None:
        assert resolve(self.versions, date(2040, 1, 1)) is self.versions[1]

    def test_before_the_first_version_there_is_none(self) -> None:
        assert resolve(self.versions, date(2025, 3, 31)) is None

    def test_drafts_and_withdrawn_versions_never_apply(self) -> None:
        assert resolve([V("DRAFT", date(2020, 1, 1))], date(2026, 1, 1)) is None
        assert resolve([V("WITHDRAWN", date(2020, 1, 1))], date(2026, 1, 1)) is None


class TestSlabProblems:
    def test_contiguous_bands_are_valid(self) -> None:
        assert (
            slab_problems(
                [slab(1, "0", "300000"), slab(2, "300000", "700000"), slab(3, "700000", None)]
            )
            == []
        )

    def test_a_gap_is_reported(self) -> None:
        problems = slab_problems([slab(1, "0", "300000"), slab(2, "400000", None)])
        assert len(problems) == 1 and "300000" in problems[0]

    def test_an_overlap_is_reported(self) -> None:
        assert slab_problems([slab(1, "0", "300000"), slab(2, "250000", None)])

    def test_an_open_ended_band_must_be_last(self) -> None:
        assert slab_problems([slab(1, "0", None), slab(2, "0", "10")])

    def test_order_follows_sequence_not_input_order(self) -> None:
        assert slab_problems([slab(2, "100", None), slab(1, "0", "100")]) == []

    def test_tables_and_conditions_are_checked_independently(self) -> None:
        slabs = [
            slab(1, "0", None, table="A"),
            slab(1, "0", "10", table="B", cond="GENDER=FEMALE"),
            slab(2, "10", None, table="B", cond="GENDER=FEMALE"),
            slab(1, "0", None, table="B", cond="GENDER=MALE"),
        ]
        assert slab_problems(slabs) == []


class TestSchemaVersion:
    @pytest.mark.parametrize(
        ("value", "family", "ok"),
        [
            ("PF.v2", "PF", True),
            ("INCOME_TAX.v10", "INCOME_TAX", True),
            ("ESI.v1", "PF", False),
            ("PF.v0", "PF", False),
            ("PF2", "PF", False),
            ("pf.v1", "PF", False),
        ],
    )
    def test_shape_and_family(self, value: str, family: str, ok: bool) -> None:
        assert schema_matches_family(value, family) is ok
