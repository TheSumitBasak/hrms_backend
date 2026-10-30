"""Employee occupancy of positions (JA §7.7, §15.3, §18.2, §18.3).

This service owns the only write path to ``employee_position_assignments``. It is called from
inside the existing employee job-change transaction, never from a position endpoint: JA §13.7
deliberately adds no ``POST /positions/{id}/assign``, because two write paths would mean two
chances to leave employee job history and position occupancy disagreeing.

The sequence for a fill, in the order JA §7.7 and §8.9 prescribe, all in the caller's single
transaction:

1. the employee row is already locked by the job-change service;
2. lock the position row and verify it is ``OPEN``;
3. validate organization consistency, the grade, and the employment-date bounds;
4. lock the affected assignment rows and **re-count** capacity inside the lock;
5. end the outgoing assignment if asked;
6. insert the new assignment;
7. let the caller update employee job attributes and write ``employee_job_history``;
8. write audit.

Step 4 is what makes two concurrent fills of the last vacancy safe: the position row lock
serialises them, and the loser's re-count sees the winner's committed row and raises
``POSITION_HEADCOUNT_EXCEEDED`` (AC-JA-11). Counting before taking the lock would let both
pass.

``active_primary_key`` is maintained here, on every status or type change, because the unique
index that enforces "one active primary assignment per employee" is only as good as the column
it indexes.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from typing import Any

from app.audit.actions import AuditAction
from app.audit.writer import AuditWriter
from app.core.clock import exclusive_end_of_inclusive_day, today_in
from app.core.context import OrgContext
from app.core.errors import (
    ErrorDetail,
    InvalidOrganizationReference,
    PositionAssignmentOverlap,
    ValidationFailed,
)
from app.core.permissions import Permission
from app.core.warnings import WarningCode, WarningCollector
from app.db.types import uuid7
from app.models.employee import Employee
from app.models.position import (
    AssignmentSource,
    AssignmentStatus,
    AssignmentType,
    EmployeePositionAssignment,
    Position,
)
from app.repositories.employee import EmployeeRepository
from app.repositories.organization import OrganizationRepository
from app.repositories.position import (
    PositionAssignmentRepository,
    PositionRepository,
)
from app.services.capacity import CapacityCalculator
from app.services.positions import PositionService


class PositionAssignmentService:
    """Occupancy writes and the employee-module hooks (JA §18.2, §18.3)."""

    def __init__(
        self,
        *,
        assignments: PositionAssignmentRepository,
        positions: PositionRepository,
        employees: EmployeeRepository,
        organizations: OrganizationRepository,
        position_service: PositionService,
        capacity: CapacityCalculator,
        audit: AuditWriter,
    ) -> None:
        self._assignments = assignments
        self._positions = positions
        self._employees = employees
        self._organizations = organizations
        self._position_service = position_service
        self._capacity = capacity
        self._audit = audit

    # ====================================================================== reads

    def list_for_employee(
        self,
        context: OrgContext,
        employee_id: uuid.UUID,
        *,
        status: Sequence[str] | None = None,
        as_of: date | None = None,
    ) -> list[tuple[EmployeePositionAssignment, Position | None]]:
        """Assignment history for one employee (JA §13.7).

        Requires both ``employee.view`` and ``position.view``: the row joins a person to a
        seat, so neither permission alone is enough.
        """
        context.require(Permission.EMPLOYEE_VIEW)
        context.require(Permission.POSITION_VIEW)
        self._employees.require(employee_id, resource="employee")
        rows = list(self._assignments.list_for_employee(employee_id, status=status, as_of=as_of))
        positions = self._positions.load_many([row.position_id for row in rows])
        return [(row, positions.get(row.position_id)) for row in rows]

    def employee_row(self, employee_id: uuid.UUID) -> Employee:
        """Load and lock the employee, for a caller that owns the transaction.

        JA §8.9 puts the employee row first in the lock order. The job-change service already
        holds that lock when it calls in; a producer at the §18.2 service boundary — an
        onboarding module, say — takes it here instead of reaching for the repository.
        """
        return self._employees.lock(employee_id)

    def current_position_id(self, employee_id: uuid.UUID) -> uuid.UUID | None:
        """The employee's active primary position, or ``None``.

        Used to snapshot ``employee_job_history.position_id`` and to default a reporting
        manager. Not a source of truth: it is derived from the assignment rows every time.
        """
        active = self._assignments.active_primary_for_employee(employee_id)
        return active.position_id if active is not None else None

    # ===================================================================== writes

    def apply_position_change(
        self,
        context: OrgContext,
        *,
        employee: Employee,
        change: dict[str, Any],
        effective_date: date,
        job_change_values: dict[str, Any],
        warnings: WarningCollector,
        source: str = AssignmentSource.MANUAL.value,
    ) -> EmployeePositionAssignment | None:
        """Apply the optional ``position_change`` of a job change (JA §7.7).

        ``job_change_values`` is the rest of the job-change payload and may be *mutated*: when
        the target position defines a grade or department the employee did not supply, those
        values are filled in so the employee record, the history row and the position stay
        consistent (JA §19.11). A supplied value that contradicts the position is rejected
        with a field-level detail rather than silently overridden.

        Returns the new assignment, or ``None`` when the change only ended occupancy.
        """
        context.require(Permission.EMPLOYEE_EDIT)
        context.require(Permission.POSITION_VIEW)

        position_id = change.get("position_id")
        end_current = bool(change.get("end_current_assignment", True))
        reason = change.get("reason")
        assignment_type = str(change.get("assignment_type") or AssignmentType.PRIMARY.value)
        requested_fte = _as_fte(change.get("fte"))
        effective_to = change.get("effective_to")

        # Step 1: the employee row is already locked by the caller. Lock their open
        # assignments next so an ending row cannot move under us (§8.9 step 4).
        open_assignments = list(self._assignments.lock_for_employee(employee.id))

        ended: list[EmployeePositionAssignment] = []
        if end_current:
            for row in open_assignments:
                if position_id is not None and row.position_id == position_id:
                    # Re-filling the same position is a change to that assignment, not a
                    # move; it is handled below by the overlap and capacity checks.
                    continue
                self._end_assignment(
                    context,
                    row,
                    effective_to=effective_date,
                    reason=str(reason or "Position change"),
                )
                ended.append(row)

        if position_id is None:
            if not ended:
                raise ValidationFailed(
                    details=[
                        ErrorDetail(
                            "position_change.position_id",
                            "REQUIRED",
                            "There is no active assignment to end.",
                        )
                    ]
                )
            return None

        # Step 2: lock the position and insist it is open for a new fill.
        position = self._position_service.require_fillable(position_id)

        # Step 3: validations that do not need the capacity count.
        self._assert_employment_window(employee, effective_date, effective_to)
        self._reconcile_job_attributes(position, job_change_values)

        if (
            assignment_type == AssignmentType.PRIMARY.value
            and not end_current
            and any(
                row.assignment_type == AssignmentType.PRIMARY.value
                and row.assignment_status == AssignmentStatus.ACTIVE.value
                for row in open_assignments
                if row not in ended
            )
        ):
            raise PositionAssignmentOverlap(
                "This employee already holds an active primary assignment."
            ).with_detail(
                field="position_change.assignment_type",
                code="POSITION_ASSIGNMENT_OVERLAP",
                message=(
                    "End the current primary assignment, or use ACTING or SECONDARY for an "
                    "additional position."
                ),
            )

        overlapping = [
            row
            for row in self._assignments.overlapping_for_employee(
                employee_id=employee.id,
                position_id=position.id,
                effective_from=effective_date,
                effective_to=effective_to,
            )
            if row not in ended
        ]
        if overlapping:
            raise PositionAssignmentOverlap(
                "An assignment to this position already covers that period."
            ).with_detail(
                field="position_change.effective_from",
                code="POSITION_ASSIGNMENT_OVERLAP",
                message="End the existing assignment before starting a new one.",
            )

        # Step 4: lock the position's assignment rows and re-count inside the lock.
        self._assignments.lock_for_position(position.id)
        status = self._status_for(effective_date)
        for moment in self._capacity_check_dates(effective_date, effective_to):
            self._capacity.assert_admits(position, requested_fte=requested_fte, as_of=moment)

        # Step 5/6: insert the new assignment.
        assignment = EmployeePositionAssignment(
            id=uuid7(),
            organization_id=context.organization_id,
            employee_id=employee.id,
            position_id=position.id,
            assignment_type=assignment_type,
            assignment_status=status,
            fte=requested_fte,
            effective_from=effective_date,
            effective_to=effective_to,
            source=source,
            created_by=context.user_id,
        )
        _sync_primary_key(assignment)
        self._assignments.add(assignment)
        self._assignments.flush()

        self._default_reporting_manager(
            context,
            employee=employee,
            position=position,
            job_change_values=job_change_values,
            warnings=warnings,
        )

        self._audit.record_change(
            context,
            action=(
                AuditAction.EMPLOYEE_POSITION_CHANGED
                if ended
                else AuditAction.EMPLOYEE_POSITION_ASSIGNED
            ),
            entity_type="position_assignment",
            entity_id=assignment.id,
            employee_id=employee.id,
            before=(
                {"position_id": ended[0].position_id, "assignment_status": "ACTIVE"}
                if ended
                else None
            ),
            after={
                "position_id": assignment.position_id,
                "assignment_status": assignment.assignment_status,
                "fte": assignment.fte,
                "effective_from": assignment.effective_from,
            },
            fields=["position_id", "assignment_status", "fte", "effective_from"],
            reason=str(reason) if reason else None,
        )
        return assignment

    def end_assignments_for_employee(
        self,
        context: OrgContext,
        *,
        employee: Employee,
        last_working_day: date | None,
        reason: str,
    ) -> list[EmployeePositionAssignment]:
        """End or cancel every open assignment when employment ends (JA §18.3).

        The exclusive bound is ``employment_end_date + 1 day``: ``employees`` stores the last
        working day inclusive, while an assignment interval is half-open, and §10.15 names
        this as the one documented conversion. Getting it wrong by a day would leave the
        position occupied on the employee's last day or free them a day early.

        A ``SCHEDULED`` assignment that has not started becomes ``CANCELLED`` rather than
        ``ENDED``: it never occupied the seat, so there is no interval to close.
        """
        exclusive_end = exclusive_end_of_inclusive_day(last_working_day) or self._today()
        ended: list[EmployeePositionAssignment] = []
        for row in self._assignments.lock_for_employee(employee.id):
            if (
                row.assignment_status == AssignmentStatus.SCHEDULED.value
                and row.effective_from >= exclusive_end
            ):
                self._cancel_assignment(context, row, reason=reason)
            else:
                self._end_assignment(
                    context,
                    row,
                    effective_to=max(exclusive_end, row.effective_from),
                    reason=reason,
                )
            ended.append(row)
        return ended

    def activate_due(
        self, context: OrgContext, *, as_of: date | None = None
    ) -> list[EmployeePositionAssignment]:
        """Promote ``SCHEDULED`` assignments whose date has arrived (JA §15.3).

        Capacity is re-checked at activation, because the position may have been filled or
        shrunk since the row was scheduled. A row that no longer fits stays ``SCHEDULED`` and
        is reported rather than silently activated over the cap.
        """
        moment = as_of or self._today()
        activated: list[EmployeePositionAssignment] = []
        for row in self._assignments.due_to_activate(as_of=moment):
            position = self._positions.lock(row.position_id)
            self._assignments.lock_for_position(position.id)
            snapshot = self._capacity.snapshot(position, as_of=moment, exclude_assignment_id=row.id)
            if not snapshot.admits(row.fte):
                continue
            row.assignment_status = AssignmentStatus.ACTIVE.value
            _sync_primary_key(row)
            row.updated_by = context.user_id
            self._audit.record_change(
                context,
                action=AuditAction.EMPLOYEE_POSITION_ACTIVATED,
                entity_type="position_assignment",
                entity_id=row.id,
                employee_id=row.employee_id,
                before={"assignment_status": AssignmentStatus.SCHEDULED.value},
                after={"assignment_status": row.assignment_status},
                fields=["assignment_status"],
            )
            activated.append(row)
        return activated

    # ------------------------------------------------------------------ helpers

    def _end_assignment(
        self,
        context: OrgContext,
        row: EmployeePositionAssignment,
        *,
        effective_to: date,
        reason: str,
    ) -> None:
        before = {
            "assignment_status": row.assignment_status,
            "effective_to": row.effective_to,
        }
        row.assignment_status = AssignmentStatus.ENDED.value
        row.effective_to = max(effective_to, row.effective_from)
        row.end_reason = reason
        _sync_primary_key(row)
        row.updated_by = context.user_id
        self._audit.record_change(
            context,
            action=AuditAction.EMPLOYEE_POSITION_ENDED,
            entity_type="position_assignment",
            entity_id=row.id,
            employee_id=row.employee_id,
            before=before,
            after={
                "assignment_status": row.assignment_status,
                "effective_to": row.effective_to,
            },
            fields=["assignment_status", "effective_to"],
            reason=reason,
        )

    def _cancel_assignment(
        self, context: OrgContext, row: EmployeePositionAssignment, *, reason: str
    ) -> None:
        before = {"assignment_status": row.assignment_status}
        row.assignment_status = AssignmentStatus.CANCELLED.value
        row.end_reason = reason
        _sync_primary_key(row)
        row.updated_by = context.user_id
        self._audit.record_change(
            context,
            action=AuditAction.EMPLOYEE_POSITION_CANCELLED,
            entity_type="position_assignment",
            entity_id=row.id,
            employee_id=row.employee_id,
            before=before,
            after={"assignment_status": row.assignment_status},
            fields=["assignment_status"],
            reason=reason,
        )

    def _capacity_check_dates(self, effective_from: date, effective_to: date | None) -> list[date]:
        """Every date on which this assignment must fit the position's capacity.

        Always its own start date: a ``SCHEDULED`` row consumes nothing today, but it has to
        fit on the day it starts, or activation would fail later — after the employee had
        already been told the transfer was accepted.

        And **also today**, when the assignment starts in the past and is still open. A
        backdated open-ended fill occupies the seat now as well as then, so checking only the
        start date would admit a position that is over its cap today: exactly the state
        JA §19.7 says must not arise and §17.5 reports as a P1 defect.
        """
        today = self._today()
        dates = [effective_from]
        still_open_today = effective_to is None or effective_to > today
        if effective_from < today and still_open_today:
            dates.append(today)
        return dates

    def _status_for(self, effective_date: date) -> str:
        """JA §15.3: a future start is ``SCHEDULED``, today or earlier is ``ACTIVE``."""
        return (
            AssignmentStatus.SCHEDULED.value
            if effective_date > self._today()
            else AssignmentStatus.ACTIVE.value
        )

    def _assert_employment_window(
        self, employee: Employee, effective_from: date, effective_to: date | None
    ) -> None:
        """JA §19.11: an assignment must sit inside the employment it belongs to."""
        if employee.joining_date is not None and effective_from < employee.joining_date:
            raise ValidationFailed(
                details=[
                    ErrorDetail(
                        "effective_date",
                        "BEFORE_JOINING",
                        "An assignment cannot start before the joining date "
                        f"({employee.joining_date.isoformat()}).",
                    )
                ]
            )
        limit = exclusive_end_of_inclusive_day(employee.employment_end_date)
        if limit is not None and effective_from >= limit:
            raise ValidationFailed(
                details=[
                    ErrorDetail(
                        "effective_date",
                        "AFTER_EMPLOYMENT_END",
                        "An assignment cannot start on or after the employment end date.",
                    )
                ]
            )
        if limit is not None and effective_to is not None and effective_to > limit:
            raise ValidationFailed(
                details=[
                    ErrorDetail(
                        "position_change.effective_to",
                        "AFTER_EMPLOYMENT_END",
                        "The assignment would outlast the employment record.",
                    )
                ]
            )

    def _reconcile_job_attributes(
        self, position: Position, job_change_values: dict[str, Any]
    ) -> None:
        """Keep the employee's job attributes compatible with the position (JA §19.11).

        Where the caller said nothing, the position's department and grade are adopted — the
        employee record and the position would otherwise disagree about where the person
        works. Where the caller said something different, that is a conflict the client has
        to resolve, not something to overwrite.
        """
        for field, position_value in (
            ("department_id", position.department_id),
            ("sub_department_id", position.sub_department_id),
            ("grade_id", position.grade_id),
        ):
            if position_value is None:
                continue
            supplied = job_change_values.get(field)
            if supplied is None:
                job_change_values[field] = position_value
            elif supplied != position_value:
                raise ValidationFailed(
                    details=[
                        ErrorDetail(
                            field,
                            "POSITION_MISMATCH",
                            f"The target position defines a different {field.removesuffix('_id')}.",
                        )
                    ]
                )

    def _default_reporting_manager(
        self,
        context: OrgContext,
        *,
        employee: Employee,
        position: Position,
        job_change_values: dict[str, Any],
        warnings: WarningCollector,
    ) -> None:
        """JA §7.5: default the manager from the parent position's incumbent — carefully.

        It must never silently overwrite an existing manager. So: set it when the employee has
        none, leave it and raise ``W-POSITION-MANAGER-DIFFERS`` when it differs, and do
        nothing at all when the caller supplied a manager explicitly.
        """
        if position.reports_to_position_id is None:
            return
        if job_change_values.get("reporting_manager_employee_id") is not None:
            return

        parent_rows = [
            row
            for row in self._assignments.active_for_position(
                position.reports_to_position_id, as_of=self._today()
            )
            if row.employee_id != employee.id
        ]
        if not parent_rows:
            return
        candidate = parent_rows[0].employee_id
        current = employee.reporting_manager_employee_id
        if current is None:
            job_change_values["reporting_manager_employee_id"] = candidate
            return
        if current != candidate:
            warnings.add(WarningCode.POSITION_MANAGER_DIFFERS)

    def _today(self) -> date:
        organization = self._organizations.get(self._assignments.organization_id)
        return today_in(organization.timezone if organization else None)


def _sync_primary_key(row: EmployeePositionAssignment) -> None:
    """Keep ``active_primary_key`` in step with the status and type it encodes.

    The database CHECK insists the two agree and the unique index enforces one active primary
    per employee, so forgetting this call turns into a constraint error rather than a silently
    broken invariant — which is the intended failure mode.
    """
    is_active_primary = (
        row.assignment_status == AssignmentStatus.ACTIVE.value
        and row.assignment_type == AssignmentType.PRIMARY.value
    )
    row.active_primary_key = row.employee_id if is_active_primary else None


def _as_fte(value: Any) -> Decimal:
    if value is None:
        return Decimal("1.00")
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def assert_same_organization(employee: Employee, position: Position) -> None:
    """A belt to the repository's braces (JA §19.11).

    Both rows come from organization-scoped repositories, so this cannot normally fire. It is
    here because a future caller that loads a row some other way would otherwise create a
    cross-tenant assignment, and the composite FK error it produced would be opaque.
    """
    if employee.organization_id != position.organization_id:
        raise InvalidOrganizationReference(
            "The employee and the position belong to different organizations."
        )


__all__ = ["PositionAssignmentService", "assert_same_organization"]
