"""The JA §8.4 headcount formulas, in one place.

JA §20.4 is explicit: *"No duplicated formulas: one module (or one service method) owns
``OccupiedFTE``, ``VacantFTE``, ``PlanAvailableFTE`` and the capacity predicate. Reports call
it; they do not re-implement it."* This module is that owner. The position service, the
assignment service, the headcount service, the summary report and the nightly reconciliation
all call :class:`CapacityCalculator`, so a report cannot disagree with the check that admitted
the data.

All arithmetic is :class:`~decimal.Decimal`. Not a stylistic choice: 0.1 + 0.2 in binary
floating point is not 0.3, and an FTE cap compared with a float sum would admit a fill that
puts a position 0.0000001 over its authorization — or reject one that exactly fits.

FTE and people are tracked **separately and both enforced** (JA §8.2, JA-Q-03). A position
authorized for 2.00 FTE across at most 3 incumbents admits two full-time people or three
part-timers summing to 2.00, and rejects four half-timers even though 4 x 0.50 = 2.00 fits the
FTE cap. Collapsing the two dimensions into one number loses exactly that case.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from app.core.errors import (
    ErrorDetail,
    HeadcountBelowAuthorized,
    HeadcountLimitExceeded,
    PositionHeadcountExceeded,
)
from app.models.position import (
    ESTABLISHMENT_POSITION_STATUSES,
    HeadcountPlan,
    Position,
    PositionStatus,
)
from app.repositories.position import (
    HeadcountPlanRepository,
    HeadcountReservationRepository,
    Occupancy,
    PositionAssignmentRepository,
    PositionRepository,
)

ZERO = Decimal("0.00")


@dataclass(frozen=True, slots=True)
class CapacitySnapshot:
    """A position's authorization, usage and remaining room on one date (JA §8.4)."""

    as_of: date
    authorized_fte: Decimal
    occupied_fte: Decimal
    reserved_fte: Decimal
    max_incumbents: int
    occupied_headcount: int
    reserved_headcount: int
    position_status: str

    @property
    def vacant_fte(self) -> Decimal:
        """``MAX(0, authorized - occupied - reserved)``. Never negative (JA §8.4)."""
        return max(ZERO, self.authorized_fte - self.occupied_fte - self.reserved_fte)

    @property
    def vacant_headcount(self) -> int:
        return max(0, self.max_incumbents - self.occupied_headcount - self.reserved_headcount)

    @property
    def is_over_capacity(self) -> bool:
        """True only if a defect has already happened — the checks exist to prevent this."""
        return (
            self.occupied_fte > self.authorized_fte or self.occupied_headcount > self.max_incumbents
        )

    @property
    def is_fillable(self) -> bool:
        """Whether this position could take another incumbent at all.

        ``admits`` answers the same question for a *specific* FTE; this is the headline a
        response carries, so a client can show "vacant" without guessing an FTE first.
        """
        return self.position_status == PositionStatus.OPEN.value and (
            self.vacant_fte > ZERO and self.vacant_headcount >= 1
        )

    def admits(self, requested_fte: Decimal) -> bool:
        """``PositionIsFillable`` (JA §8.4): open, and room in **both** dimensions."""
        if self.position_status != PositionStatus.OPEN.value:
            return False
        fte_room = self.authorized_fte - self.occupied_fte - self.reserved_fte
        headcount_room = self.max_incumbents - self.occupied_headcount - self.reserved_headcount
        return fte_room >= requested_fte and headcount_room >= 1


@dataclass(frozen=True, slots=True)
class PlanAvailability:
    """What a scoped plan still allows (JA §8.4 ``PlanAvailable*``)."""

    plan: HeadcountPlan
    committed_fte: Decimal
    committed_headcount: int

    @property
    def available_fte(self) -> Decimal:
        return max(ZERO, self.plan.authorized_fte - self.committed_fte)

    @property
    def available_headcount(self) -> int:
        return max(0, self.plan.authorized_headcount - self.committed_headcount)

    @property
    def is_breached(self) -> bool:
        return (
            self.committed_fte > self.plan.authorized_fte
            or self.committed_headcount > self.plan.authorized_headcount
        )


class CapacityCalculator:
    """Reads occupancy and reservations and answers capacity questions.

    Read-only: it never writes a row and never opens a transaction. Callers that are about to
    change capacity must already hold the position row lock before asking, because an answer
    computed outside the lock is advisory only (JA §8.9).
    """

    def __init__(
        self,
        *,
        positions: PositionRepository,
        assignments: PositionAssignmentRepository,
        reservations: HeadcountReservationRepository,
        plans: HeadcountPlanRepository,
    ) -> None:
        self._positions = positions
        self._assignments = assignments
        self._reservations = reservations
        self._plans = plans

    # ------------------------------------------------------------- position level

    def snapshot(
        self,
        position: Position,
        *,
        as_of: date,
        exclude_assignment_id: uuid.UUID | None = None,
    ) -> CapacitySnapshot:
        """One position's figures.

        ``exclude_assignment_id`` applies the JA §8.4 rule for a *changed* assignment: the row
        being replaced leaves occupancy before the new FTE is measured, so reducing 1.00 to
        0.50 is not rejected against its own 1.00.
        """
        occupied = self._assignments.occupancy_for(
            position.id, as_of=as_of, exclude_assignment_id=exclude_assignment_id
        )
        reserved = self._reservations.reserved_for(position.id, as_of=as_of)
        return CapacitySnapshot(
            as_of=as_of,
            authorized_fte=position.authorized_fte,
            occupied_fte=occupied.fte,
            reserved_fte=reserved.fte,
            max_incumbents=position.max_incumbents,
            occupied_headcount=occupied.headcount,
            reserved_headcount=reserved.headcount,
            position_status=position.position_status,
        )

    def snapshot_map(
        self, positions: Sequence[Position], *, as_of: date
    ) -> dict[uuid.UUID, CapacitySnapshot]:
        """Figures for many positions in two grouped queries — never one query per row."""
        position_ids = [position.id for position in positions]
        occupied = self._assignments.occupancy_map(position_ids, as_of=as_of)
        reserved = self._reservations.reserved_map(position_ids, as_of=as_of)
        empty = Occupancy()
        return {
            position.id: CapacitySnapshot(
                as_of=as_of,
                authorized_fte=position.authorized_fte,
                occupied_fte=occupied.get(position.id, empty).fte,
                reserved_fte=reserved.get(position.id, empty).fte,
                max_incumbents=position.max_incumbents,
                occupied_headcount=occupied.get(position.id, empty).headcount,
                reserved_headcount=reserved.get(position.id, empty).headcount,
                position_status=position.position_status,
            )
            for position in positions
        }

    def assert_admits(
        self,
        position: Position,
        *,
        requested_fte: Decimal,
        as_of: date,
        exclude_assignment_id: uuid.UUID | None = None,
        field: str = "position_change.fte",
    ) -> CapacitySnapshot:
        """Raise ``POSITION_HEADCOUNT_EXCEEDED`` unless the position admits ``requested_fte``.

        The message carries authorized, occupied and requested values, because JA §20.9
        requires a numeric headcount error to be explainable to the person who hit it.
        """
        snapshot = self.snapshot(position, as_of=as_of, exclude_assignment_id=exclude_assignment_id)
        fte_room = snapshot.authorized_fte - snapshot.occupied_fte - snapshot.reserved_fte
        headcount_room = (
            snapshot.max_incumbents - snapshot.occupied_headcount - snapshot.reserved_headcount
        )
        if fte_room < requested_fte:
            raise PositionHeadcountExceeded(
                "Filling this position would exceed its authorized FTE."
            ).with_detail(
                field=field,
                code="POSITION_HEADCOUNT_EXCEEDED",
                message=(
                    f"Authorized {snapshot.authorized_fte:.2f} FTE; occupied "
                    f"{snapshot.occupied_fte:.2f} FTE; reserved "
                    f"{snapshot.reserved_fte:.2f} FTE; requested {requested_fte:.2f} FTE."
                ),
            )
        if headcount_room < 1:
            raise PositionHeadcountExceeded(
                "Filling this position would exceed its incumbent capacity."
            ).with_detail(
                field=field,
                code="POSITION_HEADCOUNT_EXCEEDED",
                message=(
                    f"Maximum {snapshot.max_incumbents} incumbent(s); occupied "
                    f"{snapshot.occupied_headcount}; reserved {snapshot.reserved_headcount}."
                ),
            )
        return snapshot

    # ----------------------------------------------------------------- plan level

    def plans_for(self, position: Position, *, as_of: date) -> list[HeadcountPlan]:
        return self._plans.active_plans_for_position(position, as_of=as_of)

    def availability(
        self,
        plan: HeadcountPlan,
        *,
        exclude_position_id: uuid.UUID | None = None,
    ) -> PlanAvailability:
        """``PlanAuthorized*`` versus the plan's cap (JA §8.4).

        A plan authorizes **establishment**, so it counts the authorized capacity of every
        matching ``OPEN``/``FROZEN`` position — not their occupancy. Ending an assignment
        therefore releases a seat but not plan capacity, which is why JA §8.6 says to close or
        shrink positions before reducing a plan.
        """
        committed = self._positions.establishment_for_plan(
            plan,
            statuses=sorted(ESTABLISHMENT_POSITION_STATUSES),
            exclude_position_id=exclude_position_id,
        )
        return PlanAvailability(
            plan=plan, committed_fte=committed.fte, committed_headcount=committed.headcount
        )

    def assert_plans_admit(
        self,
        position: Position,
        *,
        as_of: date,
        proposed_fte: Decimal,
        proposed_headcount: int,
        field: str = "authorized_fte",
    ) -> None:
        """Every matching active plan must admit this position's proposed establishment.

        Matching is independent per plan and all matches must hold, so an organization plan
        and a department plan both apply and the tightest one decides (JA §8.6). The plans are
        locked in ascending id order first — step 3 of the JA §8.9 lock order — so a
        concurrent opening elsewhere in the same scope cannot slip past this check.
        """
        plans = self.plans_for(position, as_of=as_of)
        if not plans:
            return
        self._plans.lock_many([plan.id for plan in plans])
        for plan in plans:
            availability = self.availability(plan, exclude_position_id=position.id)
            if availability.available_fte < proposed_fte:
                raise HeadcountLimitExceeded(
                    "This would exceed the authorized FTE of a headcount plan."
                ).with_detail(
                    field=field,
                    code="HEADCOUNT_LIMIT_EXCEEDED",
                    message=(
                        f"Plan {plan.plan_code} authorizes "
                        f"{plan.authorized_fte:.2f} FTE; "
                        f"{availability.committed_fte:.2f} FTE is already authorized; "
                        f"this position requests {proposed_fte:.2f} FTE."
                    ),
                )
            if availability.available_headcount < proposed_headcount:
                raise HeadcountLimitExceeded(
                    "This would exceed the authorized headcount of a headcount plan."
                ).with_detail(
                    field="max_incumbents",
                    code="HEADCOUNT_LIMIT_EXCEEDED",
                    message=(
                        f"Plan {plan.plan_code} authorizes "
                        f"{plan.authorized_headcount} incumbent(s); "
                        f"{availability.committed_headcount} already authorized; "
                        f"this position requests {proposed_headcount}."
                    ),
                )

    def assert_plan_not_below_establishment(
        self,
        plan: HeadcountPlan,
        *,
        proposed_fte: Decimal,
        proposed_headcount: int,
    ) -> None:
        """JA §8.6: a plan may not be set below what it already authorizes.

        ``HEADCOUNT_BELOW_AUTHORIZED`` rather than a warning: silently allowing it would put
        the organization in a state the reconciliation report reports as a P1 defect.
        """
        availability = self.availability(plan)
        details: list[ErrorDetail] = []
        if proposed_fte < availability.committed_fte:
            details.append(
                ErrorDetail(
                    "requested_fte",
                    "HEADCOUNT_BELOW_AUTHORIZED",
                    f"{availability.committed_fte:.2f} FTE is already authorized by matching "
                    "open or frozen positions.",
                )
            )
        if proposed_headcount < availability.committed_headcount:
            details.append(
                ErrorDetail(
                    "requested_headcount",
                    "HEADCOUNT_BELOW_AUTHORIZED",
                    f"{availability.committed_headcount} incumbent(s) are already authorized "
                    "by matching open or frozen positions.",
                )
            )
        if details:
            raise HeadcountBelowAuthorized(
                "The plan would fall below the establishment it already authorizes. "
                "Close or shrink positions first.",
                details=details,
            )


__all__ = ["CapacityCalculator", "CapacitySnapshot", "PlanAvailability"]
