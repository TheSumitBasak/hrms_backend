"""Pure business rules of the job architecture module (JA §21.2).

No database. Everything here is a rule that can be stated and checked on its own: the grade
range comparison, the headcount formulas, the lifecycle matrix, the competency union and the
half-open interval convention. The §21.3 integration suites then verify the same rules through
the API, where the database constraints and locks also apply.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest

from app.core.clock import exclusive_end_of_inclusive_day, interval_contains
from app.core.errors import EmsError, ErrorCode, InvalidGradeRange
from app.models.position import (
    ACTIVE_ASSIGNMENT_STATUSES,
    ESTABLISHMENT_POSITION_STATUSES,
    JOB_FAMILY_MAX_DEPTH,
    MAX_PROFICIENCY,
    MIN_PROFICIENCY,
    POSITION_REPORTING_MAX_DEPTH,
    AssignmentStatus,
    CompetencyRequirementType,
    PositionStatus,
)
from app.services.capacity import CapacitySnapshot, PlanAvailability
from app.services.competencies import EffectiveRequirement, merge_requirements
from app.services.job_catalogue import validate_grade_range
from app.services.positions import ALLOWED_TRANSITIONS


class _Grade:
    """A stand-in for a ``grades`` row: only ``rank`` matters to the range rule."""

    def __init__(self, rank: int) -> None:
        self.rank = rank


def _snapshot(
    *,
    authorized: str = "1.00",
    occupied: str = "0.00",
    reserved: str = "0.00",
    max_incumbents: int = 1,
    occupied_headcount: int = 0,
    reserved_headcount: int = 0,
    status: str = PositionStatus.OPEN.value,
) -> CapacitySnapshot:
    return CapacitySnapshot(
        as_of=date(2026, 6, 1),
        authorized_fte=Decimal(authorized),
        occupied_fte=Decimal(occupied),
        reserved_fte=Decimal(reserved),
        max_incumbents=max_incumbents,
        occupied_headcount=occupied_headcount,
        reserved_headcount=reserved_headcount,
        position_status=status,
    )


def _requirement(
    competency_id: uuid.UUID,
    *,
    requirement_type: str = CompetencyRequirementType.REQUIRED.value,
    proficiency: int = 3,
    source: str = "JOB",
    display_order: int | None = None,
) -> EffectiveRequirement:
    return EffectiveRequirement(
        competency_id=competency_id,
        requirement_type=requirement_type,
        min_proficiency=proficiency,
        source=source,
        mapping_id=uuid.uuid4(),
        display_order=display_order,
        effective_from=date(2026, 1, 1),
        effective_to=None,
        row_version=1,
    )


class TestGradeRange:
    """JA-GRD-002/003: the range is inclusive by ``rank``; lower rank is more junior."""

    def test_range_in_order_is_accepted(self) -> None:
        validate_grade_range(default_grade=_Grade(3), min_grade=_Grade(1), max_grade=_Grade(5))

    def test_min_outranking_max_is_rejected(self) -> None:
        with pytest.raises(InvalidGradeRange) as exc:
            validate_grade_range(default_grade=None, min_grade=_Grade(5), max_grade=_Grade(1))
        assert exc.value.code == ErrorCode.INVALID_GRADE_RANGE
        assert exc.value.details[0].field == "min_grade_id"

    def test_default_below_the_range_is_rejected(self) -> None:
        with pytest.raises(InvalidGradeRange):
            validate_grade_range(default_grade=_Grade(1), min_grade=_Grade(3), max_grade=_Grade(5))

    def test_default_above_the_range_is_rejected(self) -> None:
        with pytest.raises(InvalidGradeRange):
            validate_grade_range(default_grade=_Grade(7), min_grade=_Grade(3), max_grade=_Grade(5))

    def test_boundaries_are_inclusive(self) -> None:
        """ "Inclusive by rank" means the bounds themselves are valid defaults."""
        validate_grade_range(default_grade=_Grade(1), min_grade=_Grade(1), max_grade=_Grade(5))
        validate_grade_range(default_grade=_Grade(5), min_grade=_Grade(1), max_grade=_Grade(5))

    def test_an_absent_range_constrains_nothing(self) -> None:
        validate_grade_range(default_grade=_Grade(9), min_grade=None, max_grade=None)

    def test_one_sided_range_is_honoured(self) -> None:
        validate_grade_range(default_grade=_Grade(9), min_grade=_Grade(3), max_grade=None)
        with pytest.raises(InvalidGradeRange):
            validate_grade_range(default_grade=_Grade(2), min_grade=_Grade(3), max_grade=None)


class TestHeadcountFormulas:
    """JA §8.4. FTE and people are separate dimensions and both are enforced."""

    def test_vacancy_is_authorized_less_occupied_and_reserved(self) -> None:
        snapshot = _snapshot(
            authorized="2.00",
            occupied="1.50",
            reserved="0.25",
            max_incumbents=3,
            occupied_headcount=2,
            reserved_headcount=1,
        )
        assert snapshot.vacant_fte == Decimal("0.25")
        assert snapshot.vacant_headcount == 0

    def test_vacancy_never_goes_negative(self) -> None:
        """JA §8.4 wraps both formulas in ``MAX(0, ...)``; a defect must not read as credit."""
        snapshot = _snapshot(
            authorized="1.00",
            occupied="1.50",
            max_incumbents=1,
            occupied_headcount=2,
        )
        assert snapshot.vacant_fte == Decimal("0.00")
        assert snapshot.vacant_headcount == 0
        assert snapshot.is_over_capacity is True

    def test_worked_example_from_the_specification(self) -> None:
        """JA §8.5 verbatim: 2.00 FTE across 3 incumbents, 1.50 occupied.

        0.50 fits; 0.75 does not, even though a people-count slot remains.
        """
        snapshot = _snapshot(
            authorized="2.00", occupied="1.50", max_incumbents=3, occupied_headcount=2
        )
        assert snapshot.vacant_fte == Decimal("0.50")
        assert snapshot.vacant_headcount == 1
        assert snapshot.admits(Decimal("0.50")) is True
        assert snapshot.admits(Decimal("0.75")) is False

    def test_people_cap_blocks_a_fill_that_fits_the_fte_cap(self) -> None:
        """Four 0.50 FTE people fit 2.00 FTE but not a three-incumbent cap (JA §8.2)."""
        snapshot = _snapshot(
            authorized="2.00", occupied="1.50", max_incumbents=3, occupied_headcount=3
        )
        assert snapshot.vacant_fte == Decimal("0.50")
        assert snapshot.admits(Decimal("0.50")) is False

    def test_only_an_open_position_is_fillable(self) -> None:
        for status in (
            PositionStatus.DRAFT.value,
            PositionStatus.FROZEN.value,
            PositionStatus.CLOSED.value,
        ):
            snapshot = _snapshot(status=status)
            assert snapshot.admits(Decimal("1.00")) is False, status
            assert snapshot.is_fillable is False, status
        assert _snapshot().admits(Decimal("1.00")) is True

    def test_a_reservation_reduces_vacancy(self) -> None:
        """JA §8.8: a reservation is not an assignment but it does consume capacity."""
        snapshot = _snapshot(authorized="1.00", reserved="1.00", reserved_headcount=1)
        assert snapshot.vacant_fte == Decimal("0.00")
        assert snapshot.admits(Decimal("1.00")) is False

    def test_arithmetic_is_decimal_not_float(self) -> None:
        """Three 0.10 FTE assignments must sum to exactly 0.30 (JA §8.2)."""
        total = sum((Decimal("0.10") for _ in range(3)), Decimal("0.00"))
        assert total == Decimal("0.30")
        snapshot = _snapshot(authorized="0.30", occupied=str(total))
        assert snapshot.vacant_fte == Decimal("0.00")

    def test_frozen_positions_keep_their_occupancy(self) -> None:
        """JA §8.3: a freeze stops new fills; it does not release capacity."""
        snapshot = _snapshot(
            authorized="1.00",
            occupied="1.00",
            occupied_headcount=1,
            status=PositionStatus.FROZEN.value,
        )
        assert snapshot.occupied_fte == Decimal("1.00")
        assert snapshot.vacant_fte == Decimal("0.00")


class TestPlanAvailability:
    """JA §8.4: a plan authorizes establishment, not occupancy."""

    class _Plan:
        def __init__(self, fte: str, headcount: int) -> None:
            self.authorized_fte = Decimal(fte)
            self.authorized_headcount = headcount
            self.plan_code = "PLN1"

    def test_available_is_cap_less_committed(self) -> None:
        availability = PlanAvailability(
            plan=self._Plan("10.00", 10),  # type: ignore[arg-type]
            committed_fte=Decimal("7.50"),
            committed_headcount=8,
        )
        assert availability.available_fte == Decimal("2.50")
        assert availability.available_headcount == 2
        assert availability.is_breached is False

    def test_availability_never_goes_negative_and_reports_a_breach(self) -> None:
        availability = PlanAvailability(
            plan=self._Plan("2.00", 2),  # type: ignore[arg-type]
            committed_fte=Decimal("3.00"),
            committed_headcount=3,
        )
        assert availability.available_fte == Decimal("0.00")
        assert availability.available_headcount == 0
        assert availability.is_breached is True

    def test_only_open_and_frozen_positions_count_as_establishment(self) -> None:
        assert set(ESTABLISHMENT_POSITION_STATUSES) == {"OPEN", "FROZEN"}
        assert PositionStatus.DRAFT.value not in ESTABLISHMENT_POSITION_STATUSES
        assert PositionStatus.CLOSED.value not in ESTABLISHMENT_POSITION_STATUSES


class TestCountingRules:
    """JA §8.3: which assignment rows count toward occupancy."""

    def test_only_active_assignments_are_counted(self) -> None:
        assert set(ACTIVE_ASSIGNMENT_STATUSES) == {AssignmentStatus.ACTIVE.value}
        for status in (
            AssignmentStatus.SCHEDULED,
            AssignmentStatus.ENDED,
            AssignmentStatus.CANCELLED,
        ):
            assert status.value not in ACTIVE_ASSIGNMENT_STATUSES


class TestLifecycleMatrix:
    """JA §15.2 and §15.7: every valid transition, and nothing else."""

    def test_the_matrix_matches_the_specification(self) -> None:
        assert dict(ALLOWED_TRANSITIONS) == {
            "DRAFT": frozenset({"OPEN"}),
            "OPEN": frozenset({"FROZEN", "CLOSED"}),
            "FROZEN": frozenset({"OPEN", "CLOSED"}),
            "CLOSED": frozenset({"OPEN"}),
        }

    def test_draft_cannot_be_closed_directly(self) -> None:
        """JA §15.7: a position must be opened before it can be closed."""
        assert "CLOSED" not in ALLOWED_TRANSITIONS["DRAFT"]

    def test_a_closed_position_can_only_be_reopened(self) -> None:
        assert ALLOWED_TRANSITIONS["CLOSED"] == frozenset({"OPEN"})

    def test_no_transition_targets_draft(self) -> None:
        """``DRAFT`` is the entry state only; nothing returns to it."""
        for targets in ALLOWED_TRANSITIONS.values():
            assert "DRAFT" not in targets


class TestCompetencyUnion:
    """JA §9.3: the position's effective requirement is the stricter of the two."""

    def test_position_requirement_raises_the_job_minimum(self) -> None:
        competency = uuid.uuid4()
        merged = merge_requirements(
            [_requirement(competency, proficiency=3)],
            [_requirement(competency, proficiency=5, source="POSITION")],
        )
        assert len(merged) == 1
        assert merged[0].min_proficiency == 5
        assert merged[0].source == "POSITION"

    def test_a_lower_position_value_cannot_lower_the_effective_requirement(self) -> None:
        """Belt and braces: the service rejects a softening payload, and if one ever reached
        the merge, the higher minimum would still win rather than the lower."""
        competency = uuid.uuid4()
        merged = merge_requirements(
            [_requirement(competency, proficiency=4)],
            [_requirement(competency, proficiency=1, source="POSITION")],
        )
        assert merged[0].min_proficiency == 4

    def test_required_beats_preferred(self) -> None:
        competency = uuid.uuid4()
        merged = merge_requirements(
            [_requirement(competency, requirement_type="REQUIRED")],
            [_requirement(competency, requirement_type="PREFERRED", source="POSITION")],
        )
        assert merged[0].requirement_type == "REQUIRED"

    def test_a_position_only_requirement_is_added(self) -> None:
        job_competency, position_competency = uuid.uuid4(), uuid.uuid4()
        merged = merge_requirements(
            [_requirement(job_competency)],
            [_requirement(position_competency, source="POSITION")],
        )
        assert {row.competency_id for row in merged} == {job_competency, position_competency}

    def test_ordering_is_deterministic(self) -> None:
        first, second = uuid.uuid4(), uuid.uuid4()
        merged = merge_requirements(
            [
                _requirement(first, display_order=2),
                _requirement(second, display_order=1),
            ],
            [],
        )
        assert [row.display_order for row in merged] == [1, 2]

    def test_proficiency_scale_is_one_to_five(self) -> None:
        """JA §9.2: a fixed scale, with no separate proficiency-level table."""
        assert (MIN_PROFICIENCY, MAX_PROFICIENCY) == (1, 5)


class TestEffectiveDateConvention:
    """JA §10.15: every interval is half-open ``[from, to)``."""

    def test_the_end_date_is_outside_the_interval(self) -> None:
        start, end = date(2026, 4, 1), date(2026, 7, 1)
        assert interval_contains(start, end, date(2026, 6, 30)) is True
        assert interval_contains(start, end, end) is False

    def test_a_successor_may_start_on_the_predecessors_end_date(self) -> None:
        """Adjacent intervals touch without overlapping, which is the point of half-open."""
        boundary = date(2026, 7, 1)
        assert interval_contains(date(2026, 4, 1), boundary, boundary) is False
        assert interval_contains(boundary, None, boundary) is True

    def test_employment_end_converts_to_an_exclusive_bound(self) -> None:
        """JA §10.15 names this the one documented inclusive-date exception.

        ``employees.employment_end_date`` is the last working day, so the assignment's
        exclusive bound is the day after — being out by one would either free the seat a day
        early or keep it occupied past the employee's last day.
        """
        assert exclusive_end_of_inclusive_day(date(2026, 3, 31)) == date(2026, 4, 1)
        assert exclusive_end_of_inclusive_day(None) is None


class TestModuleBoundaries:
    """JA §1.3 and §24.5: the limits this module is defined by."""

    def test_depth_limits_are_the_specified_ones(self) -> None:
        assert JOB_FAMILY_MAX_DEPTH == 2
        assert POSITION_REPORTING_MAX_DEPTH == 50

    def test_no_employee_competency_model_exists(self) -> None:
        """JA §9.4: competencies are requirements. An assessment table is out of scope."""
        from app.db.base import Base
        from app.models import import_all_models

        import_all_models()
        forbidden = {
            "employee_competencies",
            "employee_competency_assessments",
            "competency_ratings",
            "job_levels",
            "job_grades",
            "grade_bands",
        }
        assert not forbidden & set(Base.metadata.tables)

    def test_employees_has_no_current_position_column(self) -> None:
        """JA §1.6: occupancy has one source of truth, and it is the assignment table."""
        from app.models.employee import Employee

        columns = set(Employee.__table__.columns.keys())
        assert "position_id" not in columns
        assert "current_position_id" not in columns

    def test_domain_errors_carry_a_client_safe_message(self) -> None:
        """JA §14.3: no SQL, no constraint name, no traceback reaches the client."""
        from app.core.errors import PositionHeadcountExceeded

        error: EmsError = PositionHeadcountExceeded()
        assert "SELECT" not in error.message.upper()
        assert "ck_" not in error.message
        assert error.http_status == 422
