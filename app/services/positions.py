"""Positions: definition, lifecycle, reporting hierarchy and history (JA §7, §15.2).

What this service guards, and why each rule is here rather than somewhere cheaper:

**Capacity is not editable here.** ``authorized_fte`` and ``max_incumbents`` are set once, at
creation, and afterwards only by an approved headcount change request (JA §8.7). The update
path rejects them with ``HEADCOUNT_CHANGE_REQUIRED`` instead of ignoring them, so a client
that tries cannot believe it succeeded.

**The two hierarchies stay apart.** ``reports_to_position_id`` is the position hierarchy;
``employees.reporting_manager_employee_id`` remains the people hierarchy (JA §7.5). Nothing
here writes the employee field — the assignment service does that, and only with an explicit
instruction or a warning.

**Lifecycle transitions are a table, not a pile of conditionals.** :data:`ALLOWED_TRANSITIONS`
is the §15.2 matrix, so an unlisted transition is refused by construction rather than by
whichever ``if`` happened to be written.

**Closing and archiving look at real occupancy.** A close requires zero ``ACTIVE`` *and*
``SCHEDULED`` assignments (JA §7.8) — a future fill is as much a reason not to close as a
current one — and an archive additionally requires no open headcount request and no active
reservation.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date, datetime
from typing import Any, cast

from app.audit.actions import AuditAction
from app.audit.writer import AuditWriter
from app.core.clock import today_in, utc_now
from app.core.context import OrgContext
from app.core.errors import (
    DuplicateResource,
    ErrorDetail,
    FieldReadOnly,
    HeadcountChangeRequired,
    InvalidOrganizationReference,
    InvalidStatusTransition,
    MasterInactive,
    PositionGradeOutOfRange,
    PositionInUse,
    PositionNotActive,
    PositionReportingCycle,
    ValidationFailed,
)
from app.core.permissions import Permission
from app.core.warnings import WarningCollector
from app.db.types import uuid7
from app.models.master import Grade
from app.models.position import (
    POSITION_REPORTING_MAX_DEPTH,
    CatalogueStatus,
    HeadcountRequestStatus,
    Job,
    Position,
    PositionStatus,
    PositionStatusHistory,
)
from app.repositories.master import GradeRepository, MasterReferenceChecker
from app.repositories.organization import (
    OrganizationRepository,
    OrganizationSettingsRepository,
)
from app.repositories.position import (
    HeadcountChangeRequestRepository,
    HeadcountReservationRepository,
    JobRepository,
    PositionAssignmentRepository,
    PositionRepository,
    PositionStatusHistoryRepository,
)
from app.services.capacity import CapacityCalculator, CapacitySnapshot
from app.services.job_catalogue import validate_grade_range
from app.services.organizations import assert_etag

POSITION_SORT_FIELDS = frozenset(
    {"position_code", "position_title", "position_status", "authorized_fte", "created_at"}
)

ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    PositionStatus.DRAFT.value: frozenset({PositionStatus.OPEN.value}),
    PositionStatus.OPEN.value: frozenset(
        {PositionStatus.FROZEN.value, PositionStatus.CLOSED.value}
    ),
    PositionStatus.FROZEN.value: frozenset(
        {PositionStatus.OPEN.value, PositionStatus.CLOSED.value}
    ),
    PositionStatus.CLOSED.value: frozenset({PositionStatus.OPEN.value}),
}
"""The JA §15.2 transition matrix. ``DRAFT -> CLOSED`` is absent on purpose: §15.7 requires a
position to be opened before it can be closed, so an unopened mistake is archived instead."""

POSITION_EDITABLE_FIELDS = frozenset(
    {
        "position_title",
        "job_id",
        "department_id",
        "sub_department_id",
        "branch_id",
        "work_location_id",
        "work_mode",
        "business_unit_id",
        "cost_center_id",
        "grade_id",
        "reports_to_position_id",
        "planned_start_date",
        "planned_end_date",
    }
)

CAPACITY_FIELDS = frozenset({"authorized_fte", "max_incumbents"})
"""Rejected by ``PATCH`` (JA §8.7). The schema omits them; this is the belt to that braces."""

_MASTER_REFERENCES: tuple[tuple[str, str], ...] = (
    ("department_id", "departments"),
    ("sub_department_id", "sub-departments"),
    ("branch_id", "branches"),
    ("work_location_id", "work-locations"),
    ("business_unit_id", "business-units"),
    ("cost_center_id", "cost-centers"),
)


class PositionService:
    """Position use cases (JA §7)."""

    def __init__(
        self,
        *,
        positions: PositionRepository,
        status_history: PositionStatusHistoryRepository,
        assignments: PositionAssignmentRepository,
        reservations: HeadcountReservationRepository,
        requests: HeadcountChangeRequestRepository,
        jobs: JobRepository,
        grades: GradeRepository,
        masters: MasterReferenceChecker,
        organizations: OrganizationRepository,
        settings: OrganizationSettingsRepository,
        capacity: CapacityCalculator,
        audit: AuditWriter,
    ) -> None:
        self._positions = positions
        self._status_history = status_history
        self._assignments = assignments
        self._reservations = reservations
        self._requests = requests
        self._jobs = jobs
        self._grades = grades
        self._masters = masters
        self._organizations = organizations
        self._settings = settings
        self._capacity = capacity
        self._audit = audit

    # ====================================================================== reads

    def list_positions(
        self,
        context: OrgContext,
        *,
        query: str | None = None,
        position_status: Sequence[str] | None = None,
        job_id: uuid.UUID | None = None,
        department_id: uuid.UUID | None = None,
        sub_department_id: uuid.UUID | None = None,
        branch_id: uuid.UUID | None = None,
        grade_id: uuid.UUID | None = None,
        reports_to_position_id: uuid.UUID | None = None,
        include_indirect: bool = False,
        has_vacancy: bool | None = None,
        as_of: date | None = None,
        include_archived: bool = False,
        sort_field: str = "position_code",
        direction: str = "asc",
        after_value: Any | None = None,
        after_id: uuid.UUID | None = None,
        limit: int = 25,
        with_total: bool = False,
    ) -> tuple[list[tuple[Position, CapacitySnapshot]], bool, int | None]:
        context.require(Permission.POSITION_VIEW)
        if include_archived:
            context.require_any(Permission.POSITION_MANAGE, Permission.HEADCOUNT_VIEW)
        moment = as_of or self.today()

        reports_to_ids: list[uuid.UUID] | None = None
        if reports_to_position_id is not None:
            parent = self._positions.require(
                reports_to_position_id, include_archived=True, resource="position"
            )
            if include_indirect:
                # The filter matches on `reports_to_position_id`, so the set of acceptable
                # parents is the position *and* its descendants: that yields the whole
                # subtree below it. Excluding the position itself would drop its direct
                # children, which is the opposite of widening the filter (§17.4).
                reports_to_ids = self._positions.subtree_ids(parent.id, max_depth=20)
            else:
                reports_to_ids = [parent.id]

        rows, has_more, total = self._positions.search_page(
            query=query,
            position_status=position_status,
            job_id=job_id,
            department_id=department_id,
            sub_department_id=sub_department_id,
            branch_id=branch_id,
            grade_id=grade_id,
            reports_to_position_ids=reports_to_ids,
            include_archived=include_archived,
            sort_field=sort_field,
            direction=direction,
            after_value=after_value,
            after_id=after_id,
            limit=limit,
            with_total=with_total,
        )
        snapshots = self._capacity.snapshot_map(rows, as_of=moment)
        items = [(position, snapshots[position.id]) for position in rows]
        if has_vacancy is not None:
            # Filtered after the page is read, because vacancy is derived from two
            # aggregates rather than stored. `with_total` stays the unfiltered count, which
            # is why the endpoint documents the flag as a filter, not a count.
            items = [
                (position, snapshot)
                for position, snapshot in items
                if _has_vacancy(snapshot) is has_vacancy
            ]
        return items, has_more, total

    def get_position(
        self, context: OrgContext, position_id: uuid.UUID, *, as_of: date | None = None
    ) -> tuple[Position, CapacitySnapshot]:
        context.require(Permission.POSITION_VIEW)
        position = self._positions.require(position_id, include_archived=True, resource="position")
        snapshot = self._capacity.snapshot(position, as_of=as_of or self.today())
        return position, snapshot

    def occupancy(
        self, context: OrgContext, position_id: uuid.UUID, *, as_of: date | None = None
    ) -> tuple[Position, CapacitySnapshot]:
        """``GET /positions/{id}/headcount``: counts only, never incumbent identity."""
        return self.get_position(context, position_id, as_of=as_of)

    def history(
        self,
        context: OrgContext,
        position_id: uuid.UUID,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """The merged JA §13.6 feed: status changes, headcount decisions and assignments."""
        context.require(Permission.POSITION_VIEW)
        position = self._positions.require(position_id, include_archived=True, resource="position")
        items: list[dict[str, Any]] = [
            {
                "kind": "STATUS",
                "occurred_at": row.effective_at,
                "from_status": row.from_status,
                "to_status": row.to_status,
                "reason": row.reason,
            }
            for row in self._status_history.list_for_position(position.id, since=since, until=until)
        ]
        for request in self._requests.list_for_position_history(position.id):
            moment = request.applied_at or request.decided_at or request.created_at
            if (since is not None and moment < since) or (until is not None and moment > until):
                continue
            items.append(
                {
                    "kind": "HEADCOUNT",
                    "occurred_at": moment,
                    "to_status": request.request_status,
                    "reason": request.reason,
                    "fte": request.requested_fte,
                    "headcount": request.requested_headcount,
                    "request_id": request.id,
                }
            )
        for assignment in self._assignments.all_for_position(position.id):
            moment = assignment.created_at
            if (since is not None and moment < since) or (until is not None and moment > until):
                continue
            items.append(
                {
                    "kind": "ASSIGNMENT",
                    "occurred_at": moment,
                    "to_status": assignment.assignment_status,
                    "reason": assignment.end_reason,
                    "employee_id": assignment.employee_id,
                    "fte": assignment.fte,
                }
            )
        items.sort(key=lambda item: item["occurred_at"])
        return items

    # ===================================================================== writes

    def create_position(
        self,
        context: OrgContext,
        *,
        values: dict[str, Any],
        acknowledged_warnings: list[str],
    ) -> tuple[Position, list[str]]:
        """Create a ``DRAFT`` position (JA §15.2). Opening it is a separate, checked step."""
        context.require(Permission.POSITION_MANAGE)
        code = str(values["position_code"])
        if self._positions.get_by_code(code) is not None:
            raise DuplicateResource("A position with that code already exists.").with_detail(
                field="position_code",
                code="DUPLICATE",
                message="Position codes are unique per organization, archived rows included.",
            )

        job = self._require_usable_job(values["job_id"])
        self._validate_master_references(values)
        self._assert_sub_department_matches(
            values.get("department_id"), values.get("sub_department_id")
        )

        grade_id = values.get("grade_id") or job.default_grade_id
        grade = self._resolve_grade(grade_id, require_active=values.get("grade_id") is not None)
        self._assert_grade_in_job_range(job, grade)

        reports_to = values.get("reports_to_position_id")
        if reports_to is not None:
            self._require_reportable_position(reports_to)

        acknowledged = WarningCollector(acknowledged_warnings).enforce()
        position = Position(
            id=uuid7(),
            organization_id=context.organization_id,
            position_code=code,
            position_title=str(values.get("position_title") or job.title),
            job_id=job.id,
            department_id=values["department_id"],
            sub_department_id=values.get("sub_department_id"),
            branch_id=values.get("branch_id"),
            work_location_id=values.get("work_location_id"),
            work_mode=str(values.get("work_mode") or "ON_SITE"),
            business_unit_id=values.get("business_unit_id"),
            cost_center_id=values.get("cost_center_id"),
            grade_id=grade_id,
            reports_to_position_id=reports_to,
            authorized_fte=values["authorized_fte"],
            max_incumbents=values["max_incumbents"],
            position_status=PositionStatus.DRAFT.value,
            planned_start_date=values.get("planned_start_date"),
            planned_end_date=values.get("planned_end_date"),
            created_by=context.user_id,
        )
        self._positions.add(position)
        self._positions.flush()
        self._record_status(context, position, from_status=None, to_status=position.position_status)
        self._audit.record_change(
            context,
            action=AuditAction.POSITION_CREATED,
            entity_type="position",
            entity_id=position.id,
            after={
                "position_code": position.position_code,
                "position_title": position.position_title,
                "job_id": position.job_id,
                "department_id": position.department_id,
                "grade_id": position.grade_id,
                "authorized_fte": position.authorized_fte,
                "max_incumbents": position.max_incumbents,
                "position_status": position.position_status,
            },
            fields=[
                "position_code",
                "position_title",
                "job_id",
                "department_id",
                "grade_id",
                "authorized_fte",
                "max_incumbents",
                "position_status",
            ],
            acknowledged_warnings=acknowledged,
        )
        return position, acknowledged

    def update_position(
        self,
        context: OrgContext,
        *,
        position_id: uuid.UUID,
        changes: dict[str, Any],
        if_match: int | None,
        acknowledged_warnings: list[str],
    ) -> tuple[Position, list[str]]:
        context.require(Permission.POSITION_MANAGE)
        supplied_capacity = CAPACITY_FIELDS & set(changes)
        if supplied_capacity:
            raise HeadcountChangeRequired(
                "Authorized FTE and incumbent capacity change only through an approved "
                "headcount change request."
            ).with_detail(
                field=sorted(supplied_capacity)[0],
                code="HEADCOUNT_CHANGE_REQUIRED",
                message="POST /headcount-change-requests targeting this position.",
            )

        position = self._positions.require(position_id, resource="position")
        assert_etag(position.row_version, if_match)
        if position.archived_at is not None:
            raise ValidationFailed("An archived position cannot be edited.")

        if "position_code" in changes and str(changes["position_code"]) != position.position_code:
            if self._assignments.all_for_position(position.id):
                raise FieldReadOnly(
                    "The position code cannot change once an assignment exists."
                ).with_detail(
                    field="position_code",
                    code="FIELD_READ_ONLY",
                    message="Create a new position instead.",
                )
            if self._positions.get_by_code(str(changes["position_code"])) is not None:
                raise DuplicateResource("A position with that code already exists.")

        applied: dict[str, Any] = {
            key: value for key, value in changes.items() if key in POSITION_EDITABLE_FIELDS
        }
        if "position_code" in changes:
            applied["position_code"] = str(changes["position_code"])

        if "job_id" in applied:
            job = self._require_usable_job(applied["job_id"])
            applied["job_id"] = job.id
        else:
            job = cast(Job, self._jobs.get(position.job_id, include_archived=True))

        self._validate_master_references(applied)
        self._assert_sub_department_matches(
            applied.get("department_id", position.department_id),
            applied.get("sub_department_id", position.sub_department_id),
        )

        effective_grade_id = applied.get("grade_id", position.grade_id)
        grade = self._resolve_grade(effective_grade_id, require_active="grade_id" in applied)
        self._assert_grade_in_job_range(job, grade)

        if "reports_to_position_id" in applied:
            target = applied["reports_to_position_id"]
            if target is not None:
                self._require_reportable_position(target)
                self._assert_no_reporting_cycle(position.id, target)

        _assert_planned_dates(
            applied.get("planned_start_date", position.planned_start_date),
            applied.get("planned_end_date", position.planned_end_date),
        )

        acknowledged = WarningCollector(acknowledged_warnings).enforce()
        before = {field: getattr(position, field) for field in applied}
        for field, value in applied.items():
            setattr(position, field, value)
        position.updated_by = context.user_id
        self._audit.record_change(
            context,
            action=AuditAction.POSITION_UPDATED,
            entity_type="position",
            entity_id=position.id,
            before=before,
            after={field: getattr(position, field) for field in applied},
            fields=list(applied),
            acknowledged_warnings=acknowledged,
        )
        return position, acknowledged

    # ================================================================= lifecycle

    def open_position(
        self,
        context: OrgContext,
        *,
        position_id: uuid.UUID,
        reason: str | None,
        headcount_change_request_id: uuid.UUID | None,
        if_match: int | None,
    ) -> Position:
        """``DRAFT``/``FROZEN``/``CLOSED -> OPEN`` (JA §15.2).

        Reopening a ``CLOSED`` position is a *new authorization*: the specification requires
        ``headcount.approve`` and an approved request for it, which is why that path is not the
        same as un-freezing.
        """
        context.require(Permission.POSITION_MANAGE)
        position = self._positions.lock(position_id)
        assert_etag(position.row_version, if_match)
        self._assert_not_archived(position)
        from_status = position.position_status
        self._assert_transition(from_status, PositionStatus.OPEN.value)

        settings = self._settings.require(context.organization_id)
        reopening_closed = from_status == PositionStatus.CLOSED.value
        if reopening_closed:
            context.require(Permission.HEADCOUNT_APPROVE)
        if reopening_closed or settings.require_position_headcount_approval:
            self._assert_approved_authorization(position, headcount_change_request_id)

        self._require_usable_job(position.job_id)
        self._masters.require_active("departments", position.department_id)
        self._capacity.assert_plans_admit(
            position,
            as_of=self.today(),
            proposed_fte=position.authorized_fte,
            proposed_headcount=position.max_incumbents,
        )

        position.position_status = PositionStatus.OPEN.value
        position.opened_at = position.opened_at or utc_now()
        position.closed_at = None
        position.close_reason = None
        position.freeze_reason = None
        position.updated_by = context.user_id
        self._record_status(
            context,
            position,
            from_status=from_status,
            to_status=position.position_status,
            reason=reason,
        )
        self._audit.record_change(
            context,
            action=(
                AuditAction.POSITION_REOPENED
                if from_status != PositionStatus.DRAFT.value
                else AuditAction.POSITION_OPENED
            ),
            entity_type="position",
            entity_id=position.id,
            before={"position_status": from_status},
            after={
                "position_status": position.position_status,
                "authorized_fte": position.authorized_fte,
                "max_incumbents": position.max_incumbents,
            },
            fields=["position_status"],
            reason=reason,
        )
        return position

    def freeze_position(
        self, context: OrgContext, *, position_id: uuid.UUID, reason: str, if_match: int | None
    ) -> Position:
        """``OPEN -> FROZEN``: no new fills, existing incumbents stay (JA §7.4).

        A freeze does **not** release capacity — occupancy and establishment are unchanged —
        which is what stops a freeze being used to slip a position past a plan cap.
        """
        context.require(Permission.POSITION_MANAGE)
        position = self._positions.lock(position_id)
        assert_etag(position.row_version, if_match)
        self._assert_not_archived(position)
        from_status = position.position_status
        self._assert_transition(from_status, PositionStatus.FROZEN.value)
        _require_reason(reason, "freeze")

        position.position_status = PositionStatus.FROZEN.value
        position.freeze_reason = reason
        position.updated_by = context.user_id
        self._record_status(
            context,
            position,
            from_status=from_status,
            to_status=position.position_status,
            reason=reason,
        )
        self._audit.record_change(
            context,
            action=AuditAction.POSITION_FROZEN,
            entity_type="position",
            entity_id=position.id,
            before={"position_status": from_status},
            after={"position_status": position.position_status},
            fields=["position_status", "freeze_reason"],
            reason=reason,
        )
        return position

    def close_position(
        self, context: OrgContext, *, position_id: uuid.UUID, reason: str, if_match: int | None
    ) -> Position:
        """``OPEN``/``FROZEN -> CLOSED``, only at zero occupancy (JA §7.8)."""
        context.require(Permission.POSITION_MANAGE)
        position = self._positions.lock(position_id)
        assert_etag(position.row_version, if_match)
        self._assert_not_archived(position)
        from_status = position.position_status
        self._assert_transition(from_status, PositionStatus.CLOSED.value)
        _require_reason(reason, "close")

        # Lock the assignment rows before counting: a fill committing between the count and
        # the status write would leave an incumbent on a closed position (JA §19.7).
        self._assignments.lock_for_position(position.id)
        occupying = self._assignments.occupying_for_position(position.id)
        if occupying:
            raise PositionInUse(
                f"{len(occupying)} active or scheduled assignment(s) remain."
            ).with_detail(
                field="position_id",
                code="POSITION_IN_USE",
                message="End or cancel those assignments before closing the position.",
            )
        active_reservations = self._reservations.active_for_position(position.id)
        if active_reservations:
            raise PositionInUse(
                f"{len(active_reservations)} active reservation(s) remain."
            ).with_detail(
                field="position_id",
                code="POSITION_IN_USE",
                message="Release those reservations before closing the position.",
            )

        position.position_status = PositionStatus.CLOSED.value
        position.close_reason = reason
        position.closed_at = utc_now()
        position.freeze_reason = None
        position.updated_by = context.user_id
        self._record_status(
            context,
            position,
            from_status=from_status,
            to_status=position.position_status,
            reason=reason,
        )
        self._audit.record_change(
            context,
            action=AuditAction.POSITION_CLOSED,
            entity_type="position",
            entity_id=position.id,
            before={"position_status": from_status},
            after={"position_status": position.position_status},
            fields=["position_status", "close_reason"],
            reason=reason,
        )
        return position

    def archive_position(
        self, context: OrgContext, *, position_id: uuid.UUID, reason: str, if_match: int | None
    ) -> Position:
        """Archive a closed position, or a draft that was never opened (JA §7.8).

        There is no restore. JA §10.14 says a position is replaced, not resurrected: bringing
        an archived seat back would blur which authorization an assignment was made under.
        """
        context.require(Permission.POSITION_MANAGE)
        position = self._positions.lock(position_id)
        assert_etag(position.row_version, if_match)
        if position.archived_at is not None:
            raise ValidationFailed("The position is already archived.")
        if position.position_status not in {
            PositionStatus.CLOSED.value,
            PositionStatus.DRAFT.value,
        }:
            raise PositionInUse(
                "Only a closed position, or a draft that was never opened, can be archived."
            ).with_detail(
                field="position_status",
                code="POSITION_IN_USE",
                message="Close the position first.",
            )
        if self._assignments.all_for_position(position.id):
            raise PositionInUse("Assignments reference this position.").with_detail(
                field="position_id",
                code="POSITION_IN_USE",
                message="A position with assignment history cannot be archived.",
            )
        open_requests = self._requests.open_for_position(position.id)
        if open_requests:
            raise PositionInUse(
                f"{len(open_requests)} open headcount change request(s) target this position."
            ).with_detail(
                field="position_id",
                code="POSITION_IN_USE",
                message="Cancel or decide those requests first.",
            )
        if self._reservations.active_for_position(position.id):
            raise PositionInUse("Active reservations reference this position.")

        position.archived_at = utc_now()
        position.archived_by = context.user_id
        position.archive_reason = reason
        position.updated_by = context.user_id
        self._audit.record_change(
            context,
            action=AuditAction.POSITION_ARCHIVED,
            entity_type="position",
            entity_id=position.id,
            after={"archived_at": position.archived_at},
            fields=["archived_at"],
            reason=reason,
        )
        return position

    # ------------------------------------------------------------------ helpers

    def today(self) -> date:
        organization = self._organizations.get(self._positions.organization_id)
        return today_in(organization.timezone if organization else None)

    def require_fillable(self, position_id: uuid.UUID) -> Position:
        """Load a position that may accept a new assignment, with its row locked.

        Used by the assignment service, which must hold this lock before counting capacity.
        """
        position = self._positions.lock(position_id)
        if position.archived_at is not None:
            raise PositionNotActive("An archived position cannot be filled.")
        if position.position_status != PositionStatus.OPEN.value:
            raise PositionNotActive(
                f"The position is {position.position_status}; only an OPEN position can be filled."
            ).with_detail(
                field="position_change.position_id",
                code="POSITION_NOT_ACTIVE",
                message="Open the position before assigning an employee.",
            )
        return position

    def _assert_transition(self, from_status: str, to_status: str) -> None:
        if to_status not in ALLOWED_TRANSITIONS.get(from_status, frozenset()):
            raise InvalidStatusTransition(
                f"A position cannot move from {from_status} to {to_status}."
            ).with_detail(
                field="position_status",
                code="INVALID_STATUS_TRANSITION",
                message=(
                    "Allowed: "
                    + ", ".join(sorted(ALLOWED_TRANSITIONS.get(from_status, frozenset())))
                    or "none"
                ),
            )

    def _assert_not_archived(self, position: Position) -> None:
        if position.archived_at is not None:
            raise ValidationFailed("An archived position cannot change status.")

    def _assert_approved_authorization(
        self, position: Position, request_id: uuid.UUID | None
    ) -> None:
        """JA §14.1: the referenced request must be approved and target this position."""
        if request_id is None:
            raise HeadcountChangeRequired(
                "Opening this position requires an approved headcount change request."
            ).with_detail(
                field="headcount_change_request_id",
                code="HEADCOUNT_CHANGE_REQUIRED",
                message="Supply the id of an approved request for this position.",
            )
        request = self._requests.get(request_id, include_archived=True)
        if request is None or request.position_id != position.id:
            raise ValidationFailed(
                details=[
                    ErrorDetail(
                        "headcount_change_request_id",
                        "INVALID",
                        "That request does not target this position.",
                    )
                ]
            )
        if request.request_status != HeadcountRequestStatus.APPROVED.value:
            raise HeadcountChangeRequired(
                "The referenced headcount change request is not approved."
            ).with_detail(
                field="headcount_change_request_id",
                code="HEADCOUNT_CHANGE_REQUIRED",
                message=f"Its status is {request.request_status}.",
            )

    def _require_usable_job(self, job_id: uuid.UUID) -> Job:
        job = self._jobs.get(job_id)
        if job is None:
            raise InvalidOrganizationReference(
                "That job does not belong to this organization."
            ).with_detail(
                field="job_id",
                code="INVALID_ORGANIZATION_REFERENCE",
                message="Jobs are organization-scoped.",
            )
        if job.status != CatalogueStatus.ACTIVE.value:
            raise MasterInactive("That job is inactive and cannot carry a new position.")
        today = self.today()
        if job.effective_from > today or (
            job.effective_to is not None and job.effective_to <= today
        ):
            raise MasterInactive(
                "That job is not effective today and cannot carry a new position."
            ).with_detail(
                field="job_id",
                code="MASTER_INACTIVE",
                message=(
                    f"Effective from {job.effective_from.isoformat()}"
                    + (
                        f" to {job.effective_to.isoformat()}"
                        if job.effective_to is not None
                        else ""
                    )
                ),
            )
        return job

    def _require_reportable_position(self, position_id: uuid.UUID) -> Position:
        target = self._positions.get(position_id)
        if target is None:
            raise InvalidOrganizationReference(
                "That reporting position does not belong to this organization."
            ).with_detail(
                field="reports_to_position_id",
                code="INVALID_ORGANIZATION_REFERENCE",
                message="Positions are organization-scoped.",
            )
        if target.position_status == PositionStatus.CLOSED.value:
            raise PositionNotActive("A closed position cannot be a reporting parent.").with_detail(
                field="reports_to_position_id",
                code="POSITION_NOT_ACTIVE",
                message="Choose an open, frozen or draft position.",
            )
        return target

    def _assert_no_reporting_cycle(self, position_id: uuid.UUID, target_id: uuid.UUID) -> None:
        """Walk up from the proposed parent; reaching this position is a cycle (JA §7.5).

        The chain is locked in ascending id order before the walk, so two concurrent edits
        cannot each see an acyclic graph and together create a cycle.
        """
        if target_id == position_id:
            raise PositionReportingCycle("A position cannot report to itself.")
        chain = self._positions.reporting_chain(target_id, max_depth=POSITION_REPORTING_MAX_DEPTH)
        self._positions.lock_many([*chain, position_id])
        chain = self._positions.reporting_chain(target_id, max_depth=POSITION_REPORTING_MAX_DEPTH)
        if position_id in chain:
            raise PositionReportingCycle(
                "That reporting position is already below this one."
            ).with_detail(
                field="reports_to_position_id",
                code="POSITION_REPORTING_CYCLE",
                message="The position hierarchy must stay a tree.",
            )
        if len(chain) >= POSITION_REPORTING_MAX_DEPTH:
            raise PositionReportingCycle(
                f"The reporting chain exceeds {POSITION_REPORTING_MAX_DEPTH} levels."
            )

    def _validate_master_references(self, values: dict[str, Any]) -> None:
        for field, table in _MASTER_REFERENCES:
            if field in values and values[field] is not None:
                try:
                    self._masters.require_active(table, values[field])
                except InvalidOrganizationReference as exc:
                    raise InvalidOrganizationReference(str(exc)).with_detail(
                        field=field,
                        code="INVALID_ORGANIZATION_REFERENCE",
                        message="The reference belongs to another organization.",
                    ) from exc

    def _assert_sub_department_matches(
        self, department_id: uuid.UUID | None, sub_department_id: uuid.UUID | None
    ) -> None:
        """§6.3: a sub-department must belong to the position's department.

        The composite FK enforces this too; the service check exists so the client gets a
        field-level error instead of a constraint failure.
        """
        if sub_department_id is None or department_id is None:
            return
        sub_department = self._masters.get("sub-departments", sub_department_id)
        if sub_department is None:
            raise InvalidOrganizationReference(
                "That sub-department does not belong to this organization."
            ).with_detail(
                field="sub_department_id",
                code="INVALID_ORGANIZATION_REFERENCE",
                message="Sub-departments are organization-scoped.",
            )
        if sub_department.department_id != department_id:
            raise ValidationFailed(
                details=[
                    ErrorDetail(
                        "sub_department_id",
                        "MISMATCH",
                        "The sub-department belongs to a different department.",
                    )
                ]
            )

    def _resolve_grade(self, grade_id: uuid.UUID | None, *, require_active: bool) -> Grade | None:
        if grade_id is None:
            return None
        grade = cast(Grade | None, self._grades.get(grade_id))
        if grade is None:
            raise InvalidOrganizationReference(
                "That grade does not belong to this organization."
            ).with_detail(
                field="grade_id",
                code="INVALID_ORGANIZATION_REFERENCE",
                message="Grades are organization-scoped.",
            )
        if require_active and grade.status != "ACTIVE":
            raise MasterInactive("That grade is inactive.").with_detail(
                field="grade_id", code="MASTER_INACTIVE", message="Reactivate it first."
            )
        return grade

    def _assert_grade_in_job_range(self, job: Job | None, grade: Grade | None) -> None:
        """JA §6.2: the effective position grade must sit inside the job's range.

        A job range change does not rewrite existing positions, so this runs on the next
        edit, fill or job change — which is where the organization is asked to resolve it.
        """
        if job is None or grade is None:
            return
        bounds = self._grade_bounds(job)
        if bounds is None:
            return
        min_grade, max_grade = bounds
        if (min_grade is not None and grade.rank < min_grade.rank) or (
            max_grade is not None and grade.rank > max_grade.rank
        ):
            raise PositionGradeOutOfRange(
                "The position grade is outside the range its job defines."
            ).with_detail(
                field="grade_id",
                code="POSITION_GRADE_OUT_OF_RANGE",
                message=(
                    f"Grade rank {grade.rank} is outside "
                    f"[{min_grade.rank if min_grade else '-'}, "
                    f"{max_grade.rank if max_grade else '-'}]."
                ),
            )
        # Also assert the job's own range is still coherent, with the same implementation
        # the job catalogue uses, so the two cannot drift apart.
        validate_grade_range(default_grade=None, min_grade=min_grade, max_grade=max_grade)

    def _grade_bounds(self, job: Job) -> tuple[Grade | None, Grade | None] | None:
        if job.min_grade_id is None and job.max_grade_id is None:
            return None
        return (
            cast(Grade | None, self._grades.get(job.min_grade_id)) if job.min_grade_id else None,
            cast(Grade | None, self._grades.get(job.max_grade_id)) if job.max_grade_id else None,
        )

    def _record_status(
        self,
        context: OrgContext,
        position: Position,
        *,
        from_status: str | None,
        to_status: str,
        reason: str | None = None,
    ) -> PositionStatusHistory:
        row = PositionStatusHistory(
            id=uuid7(),
            organization_id=context.organization_id,
            position_id=position.id,
            from_status=from_status,
            to_status=to_status,
            effective_at=utc_now(),
            reason=reason,
            created_by=context.user_id,
        )
        self._status_history.add(row)
        return row


def _has_vacancy(snapshot: CapacitySnapshot) -> bool:
    """JA §17.4: vacant means ``OPEN`` with room in either dimension."""
    return snapshot.position_status == PositionStatus.OPEN.value and (
        snapshot.vacant_fte > 0 or snapshot.vacant_headcount > 0
    )


def _require_reason(reason: str | None, action: str) -> None:
    if not reason or len(reason.strip()) < 10:
        raise ValidationFailed(
            details=[
                ErrorDetail(
                    "reason",
                    "REQUIRED",
                    f"A reason of at least ten characters is required to {action} a position.",
                )
            ]
        )


def _assert_planned_dates(start: date | None, end: date | None) -> None:
    if start is not None and end is not None and end <= start:
        raise ValidationFailed(
            details=[
                ErrorDetail(
                    "planned_end_date",
                    "INVALID_RANGE",
                    "planned_end_date must be after planned_start_date.",
                )
            ]
        )


__all__ = ["ALLOWED_TRANSITIONS", "POSITION_SORT_FIELDS", "PositionService"]
