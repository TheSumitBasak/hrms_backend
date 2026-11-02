"""Pure rules for statutory rule versions: resolution (PCFG-20) and slab validity (§4.6)."""

from __future__ import annotations

import itertools
import re
from collections import defaultdict
from collections.abc import Iterable
from datetime import date
from decimal import Decimal
from typing import Protocol

SCHEMA_VERSION_PATTERN = re.compile(r"^([A-Z_]+)\.v([1-9][0-9]*)$")
"""``{FAMILY}.v{n}`` (§4.6). The per-version parameter registry arrives with the first rule
seed; until then only the shape and the family are checked."""


class _Versioned(Protocol):
    status: str
    effective_from: date
    effective_to: date | None


class _Slab(Protocol):
    slab_table_code: str
    condition_code: str
    sequence_no: int
    lower_bound: Decimal
    upper_bound: Decimal | None


def resolve[V: _Versioned](versions: Iterable[V], on: date) -> V | None:
    """The PUBLISHED version with ``effective_from <= on < coalesce(effective_to, inf)``."""
    for version in versions:
        if (
            version.status == "PUBLISHED"
            and version.effective_from <= on
            and (version.effective_to is None or on < version.effective_to)
        ):
            return version
    return None


def schema_matches_family(schema_version: str, family: str) -> bool:
    match = SCHEMA_VERSION_PATTERN.match(schema_version)
    return match is not None and match.group(1) == family


def slab_problems(slabs: Iterable[_Slab]) -> list[str]:
    """Slabs of one table and condition must be contiguous and non-overlapping (§4.6)."""
    groups: dict[tuple[str, str], list[_Slab]] = defaultdict(list)
    for slab in slabs:
        groups[(slab.slab_table_code, slab.condition_code)].append(slab)
    problems: list[str] = []
    for (table, condition), rows in sorted(groups.items()):
        label = f"{table}[{condition}]" if condition else table
        rows.sort(key=lambda slab: slab.sequence_no)
        for earlier, later in itertools.pairwise(rows):
            if earlier.upper_bound is None:
                problems.append(f"{label}: an open-ended band must be the last one")
            elif later.lower_bound != earlier.upper_bound:
                problems.append(
                    f"{label}: band {later.sequence_no} must start at {earlier.upper_bound}"
                )
    return problems
