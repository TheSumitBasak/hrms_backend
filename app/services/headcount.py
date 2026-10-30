"""Headcount plans, change requests, reservations and reports (JA §8, §17.5).

This service is the only path by which an authorized capacity changes after a position is
created. JA §8.7 is deliberate about that: a budget that can be edited directly is not a
budget. So ``PATCH /positions/{id}`` refuses ``authorized_fte``, and the increase arrives here
as a request that somebody else approves.

**Maker-checker is enforced three times over.** The service compares the approver with the
creator and the submitter; two database CHECKs do the same, so a direct ``UPDATE`` cannot
approve its own request either (the arrangement §17.4 already uses for salary). An
organization with only one eligible approver therefore cannot approve anything — which is the
intended outcome, and the fix is access administration, not an exception.

**Approval re-validates under a lock.** The request stores ``current_fte`` as a snapshot for
history, but approval re-reads the live target inside the transaction before applying
(JA §8.7). A decrease racing a fill is decided by whichever takes the lock first, and the
loser gets a domain error rather than a state where occupancy exceeds authorization.

**Reports and checks share one calculator.** Every figure here comes from
:class:`app.services.capacity.CapacityCalculator` or from one grouped SQL statement using the
same §8.3 predicates, so the summary, the reconciliation report and the fill check cannot
disagree (JA §17.6).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from app.audit.actions import AuditAction
from app.audit.writer import AuditWriter
from app.core.clock import today_in, utc_now
from app.core.context import OrgContext
from app.core.errors import (
    DuplicateResource,
    ErrorDetail,
    HeadcountBelowOccupied,
    HeadcountChangeNotApprovable,
    HeadcountChangeNotEditable,
    HeadcountPlanOverlap,
    InvalidOrganizationReference,
    InvalidStatusTransition,
    PositionInUse,
    PositionNotActive,
    SelfApprovalNotAllowed,
    ValidationFailed,
)
from app.core.permissions import Permission
from app.db.types import uuid7
from app.models.position import (
    ESTABLISHMENT_POSITION_STATUSES,
    HeadcountChangeRequest,
    HeadcountChangeType,
    HeadcountPlan,
    HeadcountPlanStatus,
    HeadcountRequestStatus,
    HeadcountReservation,
    HeadcountScopeType,
    Position,
    PositionStatus,
    ReservationStatus,
)
from app.repositories.master import MasterReferenceChecker
from app.repositories.organization import (
    OrganizationRepository,
    OrganizationSettingsRepository,
)
from app.repositories.position import (
    HeadcountChangeRequestRepository,
    HeadcountPlanRepository,
    HeadcountReservationRepository,
    JobRepository,
    PositionAssignmentRepository,
    PositionRepository,
    SummaryRow,
)
from app.services.capacity import CapacityCalculator
from app.services.organizations import assert_etag

GROUP_BY_VALUES = frozenset({"ORGANIZATION", "DEPARTMENT", "BRANCH", "JOB", "GRADE", "POSITION"})

_SCOPE_REFERENCES: dict[str, tuple[str, str]] = {
    HeadcountScopeType.DEPARTMENT.value: ("department_id", "departments"),
    HeadcountScopeType.BRANCH.value: ("branch_id", "branches"),
    HeadcountScopeType.WORK_LOCATION.value: ("work_location_id", "work-locations"),
    HeadcountScopeType.GRADE.value: ("grade_id", "grades"),
}

TERMINAL_REQUEST_STATUSES = frozenset(
    {
        HeadcountRequestStatus.APPROVED.value,
        HeadcountRequestStatus.REJECTED.value,
        HeadcountRequestStatus.CANCELLED.value,
    }
)


class HeadcountService:
    """Plans, change requests, reservations and the two reports (JA §8)."""

    def __init__(
        self,
        *,
        plans: HeadcountPlanRepository,
        requests: HeadcountChangeRequestRepository,
        reservations: HeadcountReservationRepository,
        positions: PositionRepository,
        assignments: PositionAssignmentRepository,
        jobs: JobRepository,
        masters: MasterReferenceChecker,
        organizations: OrganizationRepository,
        settings: OrganizationSettingsRepository,
        capacity: CapacityCalculator,
        audit: AuditWriter,
    ) -> None:
        self._plans = plans
        self._requests = requests
        self._reservations = reservations
        self._positions = positions
        self._assignments = assignments
        self._jobs = jobs
        self._masters = masters
        self._organizations = organizations
        self._settings = settings
        self._capacity = capacity
        self._audit = audit

    # ====================================================================== plans

    def list_plans(
        self,
        context: OrgContext,
        *,
        scope_type: str | None = None,
        scope_id: uuid.UUID | None = None,
        plan_status: Sequence[str] | None = None,
        effective_on: date | None = None,
        include_archived: bool = False,
        sort_field: str = "effective_from",
        direction: str = "desc",
        after_value: Any | None = None,
        after_id: uuid.UUID | None = None,
        limit: int = 25,
        with_total: bool = False,
    ) -> tuple[list[HeadcountPlan], bool, int | None]:
        context.require(Permission.HEADCOUNT_VIEW)
        return self._plans.list_plans(
            scope_type=scope_type,
            scope_id=scope_id,
            plan_status=plan_status,
            effective_on=effective_on,
            include_archived=include_archived,
            sort_field=sort_field,
            direction=direction,
            after_value=after_value,
            after_id=after_id,
            limit=limit,
            with_total=with_total,
        )

    def get_plan(self, context: OrgContext, plan_id: uuid.UUID) -> HeadcountPlan:
        context.require(Permission.HEADCOUNT_VIEW)
        return self._plans.require(plan_id, include_archived=True, resource="headcount plan")

    def plan_availability(
        self, context: OrgContext, plan_id: uuid.UUID
    ) -> tuple[HeadcountPlan, Decimal, int]:
        """A plan plus the establishment already committed against it (JA §8.4)."""
        plan = self.get_plan(context, plan_id)
        availability = self._capacity.availability(plan)
        return plan, availability.committed_fte, availability.committed_headcount

    def create_plan(self, context: OrgContext, *, values: dict[str, Any]) -> HeadcountPlan:
        """Create a ``DRAFT`` plan. A draft is not enforced until activated (JA §8.6)."""
        context.require(Permission.HEADCOUNT_MANAGE)
        code = str(values["plan_code"])
        if self._plans.get_by_code(code) is not None:
            raise DuplicateResource("A headcount plan with that code already exists.").with_detail(
                field="plan_code",
                code="DUPLICATE",
                message="Plan codes are unique per organization.",
            )
        scope_type = str(values["scope_type"])
        self._validate_scope(scope_type, values)

        plan = HeadcountPlan(
            id=uuid7(),
            organization_id=context.organization_id,
            plan_code=code,
            name=str(values["name"]),
            description=_text_or_none(values.get("description")),
            scope_type=scope_type,
            department_id=values.get("department_id"),
            branch_id=values.get("branch_id"),
            work_location_id=values.get("work_location_id"),
            job_id=values.get("job_id"),
            grade_id=values.get("grade_id"),
            authorized_fte=values["authorized_fte"],
            authorized_headcount=values["authorized_headcount"],
            plan_status=HeadcountPlanStatus.DRAFT.value,
            effective_from=values["effective_from"],
            effective_to=values.get("effective_to"),
            created_by=context.user_id,
        )
        self._plans.add(plan)
        self._plans.flush()
        self._audit.record_change(
            context,
            action=AuditAction.HEADCOUNT_PLAN_CREATED,
            entity_type="headcount_plan",
            entity_id=plan.id,
            after={
                "plan_code": plan.plan_code,
                "scope_type": plan.scope_type,
                "authorized_fte": plan.authorized_fte,
                "authorized_headcount": plan.authorized_headcount,
                "plan_status": plan.plan_status,
            },
            fields=[
                "plan_code",
                "scope_type",
                "authorized_fte",
                "authorized_headcount",
                "plan_status",
            ],
        )
        return plan

    def update_plan(
        self,
        context: OrgContext,
        *,
        plan_id: uuid.UUID,
        changes: dict[str, Any],
        if_match: int | None,
    ) -> HeadcountPlan:
        """Edit a ``DRAFT`` plan. An active plan changes only through a change request."""
        context.require(Permission.HEADCOUNT_MANAGE)
        plan = self._plans.lock(plan_id)
        assert_etag(plan.row_version, if_match)
        if plan.plan_status != HeadcountPlanStatus.DRAFT.value:
            raise HeadcountChangeNotEditable(
                f"A {plan.plan_status} plan cannot be edited directly."
            ).with_detail(
                field="plan_status",
                code="HEADCOUNT_CHANGE_NOT_EDITABLE",
                message="Submit a headcount change request against the active plan.",
            )

        applied = {
            key: value
            for key, value in changes.items()
            if key
            in {
                "name",
                "description",
                "authorized_fte",
                "authorized_headcount",
                "effective_from",
                "effective_to",
            }
        }
        effective_from = applied.get("effective_from", plan.effective_from)
        effective_to = applied.get("effective_to", plan.effective_to)
        if effective_to is not None and effective_to <= effective_from:
            raise ValidationFailed(
                details=[
                    ErrorDetail(
                        "effective_to",
                        "INVALID_RANGE",
                        "effective_to must be after effective_from.",
                    )
                ]
            )

        before = {field: getattr(plan, field) for field in applied}
        for field, value in applied.items():
            setattr(plan, field, value)
        plan.updated_by = context.user_id
        self._audit.record_change(
            context,
            action=AuditAction.HEADCOUNT_PLAN_UPDATED,
            entity_type="headcount_plan",
            entity_id=plan.id,
            before=before,
            after={field: getattr(plan, field) for field in applied},
            fields=list(applied),
        )
        return plan

    def activate_plan(
        self,
        context: OrgContext,
        *,
        plan_id: uuid.UUID,
        note: str | None,
        if_match: int | None,
    ) -> HeadcountPlan:
        """``DRAFT -> ACTIVE``: the point at which a plan becomes an enforceable cap.

        Three things must hold (JA §15.4): the approver differs from the creator, no other
        active plan covers the same exact scope for an overlapping period, and the cap is not
        already below the establishment it would govern.
        """
        context.require(Permission.HEADCOUNT_APPROVE)
        plan = self._plans.lock(plan_id)
        assert_etag(plan.row_version, if_match)
        if plan.plan_status != HeadcountPlanStatus.DRAFT.value:
            raise InvalidStatusTransition(f"A {plan.plan_status} plan cannot be activated.")
        if plan.created_by is not None and plan.created_by == context.user_id:
            raise SelfApprovalNotAllowed(
                "A headcount plan must be activated by someone other than its creator."
            )

        overlapping = self._plans.overlapping_active(
            scope_key=plan.scope_key,
            effective_from=plan.effective_from,
            effective_to=plan.effective_to,
            exclude_id=plan.id,
        )
        if overlapping:
            raise HeadcountPlanOverlap(
                "Another active plan already covers this scope for that period."
            ).with_detail(
                field="effective_from",
                code="HEADCOUNT_PLAN_OVERLAP",
                message=f"Plan {overlapping[0].plan_code} overlaps it.",
            )
        self._capacity.assert_plan_not_below_establishment(
            plan,
            proposed_fte=plan.authorized_fte,
            proposed_headcount=plan.authorized_headcount,
        )

        before = {"plan_status": plan.plan_status}
        plan.plan_status = HeadcountPlanStatus.ACTIVE.value
        plan.approved_by = context.user_id
        plan.approved_at = utc_now()
        plan.updated_by = context.user_id
        self._audit.record_change(
            context,
            action=AuditAction.HEADCOUNT_PLAN_ACTIVATED,
            entity_type="headcount_plan",
            entity_id=plan.id,
            before=before,
            after={
                "plan_status": plan.plan_status,
                "authorized_fte": plan.authorized_fte,
                "authorized_headcount": plan.authorized_headcount,
            },
            fields=["plan_status", "approved_by", "approved_at"],
            reason=note,
        )
        return plan

    def archive_plan(
        self, context: OrgContext, *, plan_id: uuid.UUID, reason: str, if_match: int | None
    ) -> HeadcountPlan:
        """Retire a plan (JA §15.4).

        ``plan_status`` carries the archive state for a plan, so ``archived_at`` stays NULL
        here (JA §10.10). A plan is never hard-deleted.
        """
        context.require_any(Permission.HEADCOUNT_MANAGE, Permission.HEADCOUNT_APPROVE)
        plan = self._plans.lock(plan_id)
        assert_etag(plan.row_version, if_match)
        if plan.plan_status == HeadcountPlanStatus.ARCHIVED.value:
            raise ValidationFailed("The plan is already archived.")
        open_requests = self._requests.open_for_plan(plan.id)
        if open_requests:
            raise PositionInUse(
                f"{len(open_requests)} open headcount change request(s) target this plan."
            ).with_detail(
                field="headcount_plan_id",
                code="POSITION_IN_USE",
                message="Decide or cancel those requests first.",
            )

        before = {"plan_status": plan.plan_status}
        plan.plan_status = HeadcountPlanStatus.ARCHIVED.value
        plan.updated_by = context.user_id
        self._audit.record_change(
            context,
            action=AuditAction.HEADCOUNT_PLAN_ARCHIVED,
            entity_type="headcount_plan",
            entity_id=plan.id,
            before=before,
            after={"plan_status": plan.plan_status},
            fields=["plan_status"],
            reason=reason,
        )
        return plan

    # ============================================================ change requests

    def list_requests(
        self,
        context: OrgContext,
        *,
        request_status: Sequence[str] | None = None,
        target_type: str | None = None,
        position_id: uuid.UUID | None = None,
        headcount_plan_id: uuid.UUID | None = None,
        sort_field: str = "created_at",
        direction: str = "desc",
        after_value: Any | None = None,
        after_id: uuid.UUID | None = None,
        limit: int = 25,
        with_total: bool = False,
    ) -> tuple[list[HeadcountChangeRequest], bool, int | None]:
        context.require(Permission.HEADCOUNT_VIEW)
        return self._requests.list_requests(
            request_status=request_status,
            target_type=target_type,
            position_id=position_id,
            headcount_plan_id=headcount_plan_id,
            sort_field=sort_field,
            direction=direction,
            after_value=after_value,
            after_id=after_id,
            limit=limit,
            with_total=with_total,
        )

    def get_request(self, context: OrgContext, request_id: uuid.UUID) -> HeadcountChangeRequest:
        context.require(Permission.HEADCOUNT_VIEW)
        return self._requests.require(
            request_id, include_archived=True, resource="headcount change request"
        )

    def create_request(
        self, context: OrgContext, *, values: dict[str, Any]
    ) -> HeadcountChangeRequest:
        """Create a ``DRAFT`` request, snapshotting the target's current capacity.

        The snapshot is history, not the basis for the decision: approval re-reads the live
        values under a lock (JA §8.7), so a stale snapshot cannot authorize more than asked.
        """
        context.require(Permission.HEADCOUNT_MANAGE)
        target = self._resolve_target(values)
        current_fte, current_headcount = _current_capacity(target)

        request = HeadcountChangeRequest(
            id=uuid7(),
            organization_id=context.organization_id,
            position_id=values.get("position_id"),
            headcount_plan_id=values.get("headcount_plan_id"),
            change_type=str(values["change_type"]),
            current_fte=current_fte,
            requested_fte=values["requested_fte"],
            current_headcount=current_headcount,
            requested_headcount=values["requested_headcount"],
            reason=str(values["reason"]),
            request_status=HeadcountRequestStatus.DRAFT.value,
            created_by=context.user_id,
        )
        self._assert_direction(request)
        self._requests.add(request)
        self._requests.flush()
        self._audit.record_change(
            context,
            action=AuditAction.HEADCOUNT_REQUEST_CREATED,
            entity_type="headcount_change_request",
            entity_id=request.id,
            after={
                "change_type": request.change_type,
                "current_fte": request.current_fte,
                "requested_fte": request.requested_fte,
                "current_headcount": request.current_headcount,
                "requested_headcount": request.requested_headcount,
                "request_status": request.request_status,
            },
            fields=[
                "change_type",
                "requested_fte",
                "requested_headcount",
                "request_status",
            ],
            reason=request.reason,
        )
        return request

    def update_request(
        self,
        context: OrgContext,
        *,
        request_id: uuid.UUID,
        changes: dict[str, Any],
        if_match: int | None,
    ) -> HeadcountChangeRequest:
        context.require(Permission.HEADCOUNT_MANAGE)
        request = self._requests.lock(request_id)
        assert_etag(request.row_version, if_match)
        self._assert_draft(request)

        applied = {
            key: value
            for key, value in changes.items()
            if key in {"change_type", "requested_fte", "requested_headcount", "reason"}
        }
        before = {field: getattr(request, field) for field in applied}
        for field, value in applied.items():
            setattr(request, field, value)
        self._assert_direction(request)
        request.updated_by = context.user_id
        self._audit.record_change(
            context,
            action=AuditAction.HEADCOUNT_REQUEST_UPDATED,
            entity_type="headcount_change_request",
            entity_id=request.id,
            before=before,
            after={field: getattr(request, field) for field in applied},
            fields=list(applied),
        )
        return request

    def submit_request(
        self, context: OrgContext, *, request_id: uuid.UUID, if_match: int | None
    ) -> HeadcountChangeRequest:
        """``DRAFT -> PENDING`` (JA §15.5)."""
        context.require(Permission.HEADCOUNT_MANAGE)
        request = self._requests.lock(request_id)
        assert_etag(request.row_version, if_match)
        self._assert_draft(request)
        target = self._load_target(request)
        if _is_archived(target):
            raise ValidationFailed("The target of this request is archived.")
        if (request.requested_fte, request.requested_headcount) == _current_capacity(target):
            raise ValidationFailed(
                details=[
                    ErrorDetail(
                        "requested_fte",
                        "NO_CHANGE",
                        "The requested values match the current authorization.",
                    )
                ]
            )

        before = {"request_status": request.request_status}
        request.request_status = HeadcountRequestStatus.PENDING.value
        request.submitted_at = utc_now()
        request.submitted_by = context.user_id
        request.updated_by = context.user_id
        self._audit.record_change(
            context,
            action=AuditAction.HEADCOUNT_REQUEST_SUBMITTED,
            entity_type="headcount_change_request",
            entity_id=request.id,
            before=before,
            after={"request_status": request.request_status},
            fields=["request_status", "submitted_at", "submitted_by"],
        )
        return request

    def approve_request(
        self,
        context: OrgContext,
        *,
        request_id: uuid.UUID,
        note: str | None,
        if_match: int | None,
    ) -> HeadcountChangeRequest:
        """``PENDING -> APPROVED`` and apply the change, in one transaction (JA §8.7).

        The order matters: lock the request, lock the target, re-validate against live
        occupancy and against every matching plan, then write. Applying first and validating
        afterwards would leave an over-authorized position behind on failure.
        """
        context.require(Permission.HEADCOUNT_APPROVE)
        request = self._requests.lock(request_id)
        assert_etag(request.row_version, if_match)
        if request.request_status != HeadcountRequestStatus.PENDING.value:
            raise HeadcountChangeNotApprovable(
                f"A {request.request_status} request cannot be approved."
            )
        self._assert_not_self_approval(context, request)

        target = self._load_target(request, lock=True)
        if _is_archived(target):
            raise ValidationFailed("The target of this request is archived.")

        if isinstance(target, Position):
            self._apply_to_position(context, request, target)
        else:
            self._apply_to_plan(context, request, target)

        before = {"request_status": request.request_status}
        request.request_status = HeadcountRequestStatus.APPROVED.value
        request.decided_by = context.user_id
        request.decided_at = utc_now()
        request.decision_note = note
        request.applied_at = utc_now()
        request.updated_by = context.user_id
        self._audit.record_change(
            context,
            action=AuditAction.HEADCOUNT_REQUEST_APPROVED,
            entity_type="headcount_change_request",
            entity_id=request.id,
            before=before,
            after={"request_status": request.request_status},
            fields=["request_status", "decided_by", "decided_at"],
            reason=note or request.reason,
        )
        self._audit.record_change(
            context,
            action=AuditAction.HEADCOUNT_REQUEST_APPLIED,
            entity_type="headcount_change_request",
            entity_id=request.id,
            after={
                "applied_at": request.applied_at,
                "requested_fte": request.requested_fte,
                "requested_headcount": request.requested_headcount,
            },
            fields=["applied_at"],
            reason=note or request.reason,
        )
        return request

    def reject_request(
        self,
        context: OrgContext,
        *,
        request_id: uuid.UUID,
        note: str,
        if_match: int | None,
    ) -> HeadcountChangeRequest:
        context.require(Permission.HEADCOUNT_APPROVE)
        request = self._requests.lock(request_id)
        assert_etag(request.row_version, if_match)
        if request.request_status != HeadcountRequestStatus.PENDING.value:
            raise HeadcountChangeNotApprovable(
                f"A {request.request_status} request cannot be rejected."
            )
        self._assert_not_self_approval(context, request)
        if not note or len(note.strip()) < 10:
            raise ValidationFailed(
                details=[
                    ErrorDetail(
                        "note",
                        "REQUIRED",
                        "A rejection requires a note of at least ten characters.",
                    )
                ]
            )

        before = {"request_status": request.request_status}
        request.request_status = HeadcountRequestStatus.REJECTED.value
        request.decided_by = context.user_id
        request.decided_at = utc_now()
        request.decision_note = note
        request.updated_by = context.user_id
        self._audit.record_change(
            context,
            action=AuditAction.HEADCOUNT_REQUEST_REJECTED,
            entity_type="headcount_change_request",
            entity_id=request.id,
            before=before,
            after={"request_status": request.request_status},
            fields=["request_status", "decided_by", "decided_at"],
            reason=note,
        )
        return request

    def cancel_request(
        self,
        context: OrgContext,
        *,
        request_id: uuid.UUID,
        reason: str,
        if_match: int | None,
    ) -> HeadcountChangeRequest:
        context.require_any(Permission.HEADCOUNT_MANAGE, Permission.HEADCOUNT_APPROVE)
        request = self._requests.lock(request_id)
        assert_etag(request.row_version, if_match)
        if request.request_status in TERMINAL_REQUEST_STATUSES:
            raise HeadcountChangeNotApprovable(
                f"A {request.request_status} request cannot be cancelled."
            )

        before = {"request_status": request.request_status}
        request.request_status = HeadcountRequestStatus.CANCELLED.value
        request.decision_note = reason
        request.updated_by = context.user_id
        self._audit.record_change(
            context,
            action=AuditAction.HEADCOUNT_REQUEST_CANCELLED,
            entity_type="headcount_change_request",
            entity_id=request.id,
            before=before,
            after={"request_status": request.request_status},
            fields=["request_status"],
            reason=reason,
        )
        return request

    # ============================================================== reservations

    def list_reservations(
        self,
        context: OrgContext,
        *,
        position_id: uuid.UUID,
        reservation_status: Sequence[str] | None = None,
    ) -> list[HeadcountReservation]:
        context.require(Permission.HEADCOUNT_VIEW)
        self._positions.require(position_id, include_archived=True, resource="position")
        return list(
            self._reservations.list_for_position(position_id, reservation_status=reservation_status)
        )

    def create_reservation(
        self, context: OrgContext, *, position_id: uuid.UUID, values: dict[str, Any]
    ) -> HeadcountReservation:
        """Hold capacity for a commitment that is not yet an employee (JA §8.8).

        Refused unless the organization has switched reservations on: with no onboarding or
        recruitment integration to release them, an always-on capacity reducer would quietly
        strand seats (JA-Q-14).
        """
        context.require(Permission.HEADCOUNT_MANAGE)
        self._assert_reservations_enabled(context)
        position = self._positions.lock(position_id)
        if position.position_status != PositionStatus.OPEN.value:
            raise PositionNotActive("Only an open position can carry a reservation.").with_detail(
                field="position_id",
                code="POSITION_NOT_ACTIVE",
                message=f"The position is {position.position_status}.",
            )

        effective_from = values.get("effective_from") or self.today()
        self._capacity.assert_admits(
            position,
            requested_fte=values["reserved_fte"],
            as_of=effective_from,
            field="reserved_fte",
        )

        reservation = HeadcountReservation(
            id=uuid7(),
            organization_id=context.organization_id,
            position_id=position.id,
            source_type=str(values["source_type"]),
            source_reference=_text_or_none(values.get("source_reference")),
            reserved_fte=values["reserved_fte"],
            reserved_headcount=values["reserved_headcount"],
            reservation_status=ReservationStatus.ACTIVE.value,
            effective_from=effective_from,
            expires_at=values["expires_at"],
            reason=_text_or_none(values.get("reason")),
            created_by=context.user_id,
        )
        self._reservations.add(reservation)
        self._reservations.flush()
        self._audit.record_change(
            context,
            action=AuditAction.HEADCOUNT_RESERVATION_CREATED,
            entity_type="headcount_reservation",
            entity_id=reservation.id,
            after={
                "position_id": reservation.position_id,
                "reserved_fte": reservation.reserved_fte,
                "reserved_headcount": reservation.reserved_headcount,
                "source_type": reservation.source_type,
            },
            fields=["position_id", "reserved_fte", "reserved_headcount", "source_type"],
        )
        return reservation

    def release_reservation(
        self,
        context: OrgContext,
        *,
        position_id: uuid.UUID,
        reservation_id: uuid.UUID,
        reason: str,
        if_match: int | None,
    ) -> HeadcountReservation:
        context.require(Permission.HEADCOUNT_MANAGE)
        self._positions.require(position_id, include_archived=True, resource="position")
        reservation = self._reservations.lock(reservation_id)
        assert_etag(reservation.row_version, if_match)
        if reservation.position_id != position_id:
            raise InvalidOrganizationReference("That reservation does not belong to this position.")
        if reservation.reservation_status != ReservationStatus.ACTIVE.value:
            raise InvalidStatusTransition(
                f"A {reservation.reservation_status} reservation cannot be released."
            )

        before = {"reservation_status": reservation.reservation_status}
        reservation.reservation_status = ReservationStatus.RELEASED.value
        reservation.reason = reason
        reservation.updated_by = context.user_id
        self._audit.record_change(
            context,
            action=AuditAction.HEADCOUNT_RESERVATION_RELEASED,
            entity_type="headcount_reservation",
            entity_id=reservation.id,
            before=before,
            after={"reservation_status": reservation.reservation_status},
            fields=["reservation_status"],
            reason=reason,
        )
        return reservation

    def expire_reservations(
        self, context: OrgContext, *, moment: datetime | None = None
    ) -> list[HeadcountReservation]:
        """Mark due reservations ``EXPIRED``. Idempotent, so the job may rerun (JA §8.8)."""
        now = moment or utc_now()
        expired: list[HeadcountReservation] = []
        for reservation in self._reservations.due_to_expire(moment=now):
            reservation.reservation_status = ReservationStatus.EXPIRED.value
            reservation.updated_by = context.user_id
            self._audit.record_change(
                context,
                action=AuditAction.HEADCOUNT_RESERVATION_EXPIRED,
                entity_type="headcount_reservation",
                entity_id=reservation.id,
                before={"reservation_status": ReservationStatus.ACTIVE.value},
                after={"reservation_status": reservation.reservation_status},
                fields=["reservation_status"],
            )
            expired.append(reservation)
        return expired

    # =================================================================== reports

    def summary(
        self,
        context: OrgContext,
        *,
        group_by: str = "ORGANIZATION",
        as_of: date | None = None,
        department_id: uuid.UUID | None = None,
        branch_id: uuid.UUID | None = None,
        job_id: uuid.UUID | None = None,
        grade_id: uuid.UUID | None = None,
        position_id: uuid.UUID | None = None,
        include_vacant_only: bool = False,
        include_draft: bool = False,
    ) -> tuple[date, list[SummaryRow]]:
        """JA §8.10: authorized versus occupied versus vacant, grouped.

        Counts only. No employee name, code or any other personal datum appears in a headcount
        report — a caller with ``headcount.view`` and nothing else must not learn who the
        incumbents are (JA §8.10).
        """
        context.require(Permission.HEADCOUNT_VIEW)
        if group_by not in GROUP_BY_VALUES:
            raise ValidationFailed(
                details=[
                    ErrorDetail(
                        "group_by",
                        "UNKNOWN_VALUE",
                        f"Allowed: {', '.join(sorted(GROUP_BY_VALUES))}.",
                    )
                ]
            )
        moment = as_of or self.today()
        self._validate_report_filters(
            department_id=department_id,
            branch_id=branch_id,
            job_id=job_id,
            grade_id=grade_id,
            position_id=position_id,
        )
        rows = self._positions.headcount_summary(
            group_by=group_by,
            as_of=moment,
            department_id=department_id,
            branch_id=branch_id,
            job_id=job_id,
            grade_id=grade_id,
            position_id=position_id,
            include_draft=include_draft,
        )
        if include_vacant_only:
            rows = [
                row
                for row in rows
                if max(
                    Decimal("0.00"),
                    row.authorized_fte - row.occupied_fte - row.reserved_fte,
                )
                > 0
                or max(
                    0,
                    row.authorized_headcount - row.occupied_headcount - row.reserved_headcount,
                )
                > 0
            ]
        return moment, rows

    def reconciliation(
        self,
        context: OrgContext,
        *,
        as_of: date | None = None,
    ) -> tuple[date, list[dict[str, Any]]]:
        """JA §17.5: the findings that should always be empty.

        Over-capacity positions, overlapping assignments, breached plans and overlapping
        plans are defects in this module, not business states, so every finding is P1 and
        nothing here repairs anything automatically.
        """
        context.require(Permission.HEADCOUNT_VIEW)
        moment = as_of or self.today()
        findings: list[dict[str, Any]] = []

        for position, occupancy in self._positions.over_capacity_positions(as_of=moment):
            if occupancy.fte > position.authorized_fte:
                findings.append(
                    {
                        "finding": "POSITION_FTE_OVER_CAPACITY",
                        "severity": "P1",
                        "position_id": position.id,
                        "authorized": position.authorized_fte,
                        "occupied": occupancy.fte,
                        "delta": occupancy.fte - position.authorized_fte,
                        "detail": f"Position {position.position_code} is over its FTE cap.",
                    }
                )
            if occupancy.headcount > position.max_incumbents:
                findings.append(
                    {
                        "finding": "POSITION_HEADCOUNT_OVER_CAPACITY",
                        "severity": "P1",
                        "position_id": position.id,
                        "authorized": Decimal(position.max_incumbents),
                        "occupied": Decimal(occupancy.headcount),
                        "delta": Decimal(occupancy.headcount - position.max_incumbents),
                        "detail": (
                            f"Position {position.position_code} has "
                            f"{occupancy.headcount} incumbents, cap "
                            f"{position.max_incumbents}."
                        ),
                    }
                )

        for plan in self._plans.active_plans(as_of=moment):
            availability = self._capacity.availability(plan)
            if availability.is_breached:
                findings.append(
                    {
                        "finding": "PLAN_OVER_AUTHORIZED",
                        "severity": "P1",
                        "headcount_plan_id": plan.id,
                        "scope_type": plan.scope_type,
                        "authorized": plan.authorized_fte,
                        "occupied": availability.committed_fte,
                        "delta": availability.committed_fte - plan.authorized_fte,
                        "detail": (
                            f"Plan {plan.plan_code} covers more establishment than it authorizes."
                        ),
                    }
                )

        for first, second in self._assignments.overlapping_pairs():
            findings.append(
                {
                    "finding": "ASSIGNMENT_OVERLAP",
                    "severity": "P1",
                    "detail": f"Assignments {first} and {second} overlap.",
                }
            )
        for first, second in self._plans.overlapping_active_pairs():
            findings.append(
                {
                    "finding": "PLAN_OVERLAP",
                    "severity": "P1",
                    "detail": f"Active plans {first} and {second} cover the same scope.",
                }
            )
        for position in self._positions.self_reporting_positions():
            findings.append(
                {
                    "finding": "POSITION_SELF_REPORTING",
                    "severity": "P1",
                    "position_id": position.id,
                    "detail": f"Position {position.position_code} reports to itself.",
                }
            )
        for assignment in self._assignments.active_for_separated_employees():
            findings.append(
                {
                    "finding": "ASSIGNMENT_FOR_SEPARATED_EMPLOYEE",
                    "severity": "P1",
                    "position_id": assignment.position_id,
                    "detail": "An assignment is still open for a separated employee.",
                }
            )
        return moment, findings

    # ------------------------------------------------------------------ helpers

    def today(self) -> date:
        organization = self._organizations.get(self._plans.organization_id)
        return today_in(organization.timezone if organization else None)

    def _assert_reservations_enabled(self, context: OrgContext) -> None:
        settings = self._settings.require(context.organization_id)
        if not settings.enable_headcount_reservations:
            raise ValidationFailed(
                details=[
                    ErrorDetail(
                        "enable_headcount_reservations",
                        "DISABLED",
                        "Headcount reservations are disabled for this organization.",
                    )
                ]
            )

    def _validate_scope(self, scope_type: str, values: dict[str, Any]) -> None:
        """One scope id, and it must exist in this organization and be active."""
        if scope_type == HeadcountScopeType.JOB.value:
            job_id: uuid.UUID | None = values.get("job_id")
            job = self._jobs.get(job_id) if job_id is not None else None
            if job is None:
                raise InvalidOrganizationReference(
                    "That job does not belong to this organization."
                ).with_detail(
                    field="job_id",
                    code="INVALID_ORGANIZATION_REFERENCE",
                    message="Jobs are organization-scoped.",
                )
            return
        reference = _SCOPE_REFERENCES.get(scope_type)
        if reference is None:
            return
        field, table = reference
        try:
            self._masters.require_active(table, values.get(field))
        except InvalidOrganizationReference as exc:
            raise InvalidOrganizationReference(str(exc)).with_detail(
                field=field,
                code="INVALID_ORGANIZATION_REFERENCE",
                message="The scope belongs to another organization.",
            ) from exc

    def _resolve_target(self, values: dict[str, Any]) -> Position | HeadcountPlan:
        if values.get("position_id") is not None:
            return self._positions.require(
                values["position_id"], include_archived=True, resource="position"
            )
        return self._plans.require(
            values["headcount_plan_id"], include_archived=True, resource="headcount plan"
        )

    def _load_target(
        self, request: HeadcountChangeRequest, *, lock: bool = False
    ) -> Position | HeadcountPlan:
        if request.position_id is not None:
            return (
                self._positions.lock(request.position_id)
                if lock
                else self._positions.require(
                    request.position_id, include_archived=True, resource="position"
                )
            )
        plan_id = request.headcount_plan_id
        assert plan_id is not None  # noqa: S101 - guaranteed by the one-target CHECK
        return (
            self._plans.lock(plan_id)
            if lock
            else self._plans.require(plan_id, include_archived=True, resource="headcount plan")
        )

    def _assert_draft(self, request: HeadcountChangeRequest) -> None:
        if request.request_status != HeadcountRequestStatus.DRAFT.value:
            raise HeadcountChangeNotEditable(
                f"A {request.request_status} request cannot be edited or submitted."
            )

    def _assert_not_self_approval(
        self, context: OrgContext, request: HeadcountChangeRequest
    ) -> None:
        """JA §8.7: the approver differs from both the creator and the submitter."""
        if context.user_id in {request.created_by, request.submitted_by}:
            raise SelfApprovalNotAllowed(
                "A headcount change must be decided by someone other than its creator or submitter."
            )

    def _assert_direction(self, request: HeadcountChangeRequest) -> None:
        """An ``INCREASE`` must increase and a ``DECREASE`` must decrease (JA §8.7)."""
        if request.change_type == HeadcountChangeType.INCREASE.value and (
            request.requested_fte < request.current_fte
            or request.requested_headcount < request.current_headcount
        ):
            raise ValidationFailed(
                details=[
                    ErrorDetail(
                        "change_type",
                        "MISMATCH",
                        "An INCREASE cannot request less than the current authorization.",
                    )
                ]
            )
        if request.change_type == HeadcountChangeType.DECREASE.value and (
            request.requested_fte > request.current_fte
            or request.requested_headcount > request.current_headcount
        ):
            raise ValidationFailed(
                details=[
                    ErrorDetail(
                        "change_type",
                        "MISMATCH",
                        "A DECREASE cannot request more than the current authorization.",
                    )
                ]
            )

    def _apply_to_position(
        self,
        context: OrgContext,
        request: HeadcountChangeRequest,
        position: Position,
    ) -> None:
        """Re-validate against live occupancy and plans, then write (JA §8.7)."""
        self._assignments.lock_for_position(position.id)
        snapshot = self._capacity.snapshot(position, as_of=self.today())
        details: list[ErrorDetail] = []
        if request.requested_fte < snapshot.occupied_fte:
            details.append(
                ErrorDetail(
                    "requested_fte",
                    "HEADCOUNT_BELOW_OCCUPIED",
                    f"{snapshot.occupied_fte:.2f} FTE is currently occupied.",
                )
            )
        if request.requested_headcount < snapshot.occupied_headcount:
            details.append(
                ErrorDetail(
                    "requested_headcount",
                    "HEADCOUNT_BELOW_OCCUPIED",
                    f"{snapshot.occupied_headcount} incumbent(s) currently hold this position.",
                )
            )
        if details:
            raise HeadcountBelowOccupied(
                "The requested capacity is below current occupancy.", details=details
            )

        if position.position_status in ESTABLISHMENT_POSITION_STATUSES:
            self._capacity.assert_plans_admit(
                position,
                as_of=self.today(),
                proposed_fte=request.requested_fte,
                proposed_headcount=request.requested_headcount,
                field="requested_fte",
            )

        before = {
            "authorized_fte": position.authorized_fte,
            "max_incumbents": position.max_incumbents,
        }
        position.authorized_fte = request.requested_fte
        position.max_incumbents = request.requested_headcount
        position.updated_by = context.user_id
        self._audit.record_change(
            context,
            action=AuditAction.POSITION_UPDATED,
            entity_type="position",
            entity_id=position.id,
            before=before,
            after={
                "authorized_fte": position.authorized_fte,
                "max_incumbents": position.max_incumbents,
            },
            fields=["authorized_fte", "max_incumbents"],
            reason=f"Headcount change request {request.id}",
        )

    def _apply_to_plan(
        self,
        context: OrgContext,
        request: HeadcountChangeRequest,
        plan: HeadcountPlan,
    ) -> None:
        self._capacity.assert_plan_not_below_establishment(
            plan,
            proposed_fte=request.requested_fte,
            proposed_headcount=request.requested_headcount,
        )
        before = {
            "authorized_fte": plan.authorized_fte,
            "authorized_headcount": plan.authorized_headcount,
        }
        plan.authorized_fte = request.requested_fte
        plan.authorized_headcount = request.requested_headcount
        plan.updated_by = context.user_id
        self._audit.record_change(
            context,
            action=AuditAction.HEADCOUNT_PLAN_UPDATED,
            entity_type="headcount_plan",
            entity_id=plan.id,
            before=before,
            after={
                "authorized_fte": plan.authorized_fte,
                "authorized_headcount": plan.authorized_headcount,
            },
            fields=["authorized_fte", "authorized_headcount"],
            reason=f"Headcount change request {request.id}",
        )

    def _validate_report_filters(
        self,
        *,
        department_id: uuid.UUID | None,
        branch_id: uuid.UUID | None,
        job_id: uuid.UUID | None,
        grade_id: uuid.UUID | None,
        position_id: uuid.UUID | None,
    ) -> None:
        """A filter id from another organization is rejected, not quietly ignored.

        Silently returning an empty report would tell a caller that another tenant's
        department has no positions, which is itself a disclosure.
        """
        if department_id is not None:
            self._masters.require_active("departments", department_id)
        if branch_id is not None and self._masters.get("branches", branch_id) is None:
            raise InvalidOrganizationReference("That branch is not in this organization.")
        if job_id is not None and self._jobs.get(job_id, include_archived=True) is None:
            raise InvalidOrganizationReference("That job is not in this organization.")
        if grade_id is not None:
            self._masters.require_active("grades", grade_id)
        if position_id is not None:
            self._positions.require(position_id, include_archived=True, resource="position")


def _current_capacity(target: Position | HeadcountPlan) -> tuple[Decimal, int]:
    if isinstance(target, Position):
        return target.authorized_fte, target.max_incumbents
    return target.authorized_fte, target.authorized_headcount


def _is_archived(target: Position | HeadcountPlan) -> bool:
    if isinstance(target, Position):
        return target.archived_at is not None
    return target.plan_status == HeadcountPlanStatus.ARCHIVED.value


def _text_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


__all__ = ["GROUP_BY_VALUES", "HeadcountService"]
