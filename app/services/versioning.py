"""Applicability matching shared by leave and attendance policy versions (6.4, ATT-07).

Both modules resolve "the version that applies to employee E on date D" the same way: ACTIVE
versions effective on D whose non-null ``applies_*`` columns equal E's attributes, ordered by
specificity DESC, priority ASC, effective_from DESC and the owning policy's code ASC.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from datetime import date
from typing import Any


def select_version[V](
    versions: Iterable[V],
    employee: Any,
    day: date,
    *,
    attributes: Sequence[str],
    policy_code: Callable[[V], str],
    extra: Callable[[V], bool] | None = None,
) -> V | None:
    """The best matching version, or ``None``.

    ``attributes`` are employee column names (``branch_id``, ``department_id`` ...); each is
    compared with the version's ``applies_<attribute>`` column, null meaning "any".
    ``extra`` carries a module-specific predicate (leave also filters on gender).
    """
    candidates = [
        v
        for v in versions
        if v.effective_from is not None  # type: ignore[attr-defined]
        and v.effective_from <= day  # type: ignore[attr-defined]
        and (v.effective_to is None or day < v.effective_to)  # type: ignore[attr-defined]
        and all(
            getattr(v, f"applies_{name}") in (None, getattr(employee, name)) for name in attributes
        )
        and (extra is None or extra(v))
    ]
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda v: (
            -v.applicability_specificity,  # type: ignore[attr-defined]
            v.priority,  # type: ignore[attr-defined]
            -v.effective_from.toordinal(),  # type: ignore[attr-defined]
            policy_code(v),
        ),
    )


def intervals_overlap(start_a: date, end_a: date | None, start_b: date, end_b: date | None) -> bool:
    """Half-open date intervals ``[start, end)`` with ``None`` meaning open-ended."""
    far = date.max
    return start_a < (end_b or far) and start_b < (end_a or far)
