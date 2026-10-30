"""Attendance regularization requests (addendum 5.14, ATT-20..23) on the shared approval model.

Lock order (7.3): employee, attendance record, then the request. Approval inserts REGULARIZATION
punches, voids targeted punches, applies ON_DUTY halves, re-evaluates the record and writes one
REGULARIZATION_APPROVED revision, all in the approver's transaction.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.audit.actions import AuditAction
from app.audit.writer import AuditWriter
from app.core.clock import today_in, utc_now
from app.core.context import OrgContext
from app.core.errors import (
    AttendanceConflictsWithLeave,
    AttendanceRecordFinalized,
    EmployeeNotEligibleForAttendance,
    InvalidApprovalTransition,
    ModuleNotEnabled,
    NoApplicableAttendancePolicy,
    NotCurrentApprover,
    PermissionDenied,
    RegularizationAlreadyPending,
    RegularizationItemsInvalid,
    RegularizationNotEnabled,
    RegularizationStale,
    RegularizationWindowExpired,
    ResourceNotFound,
    SelfApprovalNotAllowed,
    SelfServiceNotEnabled,
    ValidationFailed,
)
from app.core.permissions import Permission
from app.db.types import uuid7
from app.models.attendance import (
    AttendancePunch,
    AttendanceRecord,
    AttendanceRegularizationAction,
    AttendanceRegularizationItem,
    AttendanceRegularizationRequest,
)
from app.repositories.attendance import (
    AttendanceRecordRepository,
    PunchRepository,
    RegularizationActionRepository,
    RegularizationItemRepository,
    RegularizationRequestRepository,
)
from app.repositories.employee import EmployeeRepository
from app.repositories.organization import OrganizationRepository, OrganizationSettingsRepository
from app.repositories.platform import UserRepository
from app.services.access import AccessService
from app.services.approvals import (
    HR_QUEUE,
    MANAGER,
    ApprovalRouter,
    Route,
    assert_distinct_approver,
    authorize_decision,
    employee_in_scope,
    levels_for,
)
from app.services.attendance_records import RecordEngine, day_window, snapshot
from app.services.attendance_rules import NoApplicablePolicy, assign_business_date
from app.services.employee_status import attendance_eligible
from app.services.organizations import assert_etag
from app.services.schedule import ScheduleService

_APPROVERS = (
    Permission.ATTENDANCE_REGULARIZE_APPROVE,
    Permission.ATTENDANCE_REGULARIZE_APPROVE_HR,
)
MAX_ITEMS = 10
COMMENT_MIN = 10


@dataclass(frozen=True, slots=True)
class RequestView:
    request: AttendanceRegularizationRequest
    items: list[AttendanceRegularizationItem]
    actions: list[AttendanceRegularizationAction]


class RegularizationService:
    def __init__(
        self,
        *,
        engine: RecordEngine,
        records: AttendanceRecordRepository,
        punches: PunchRepository,
        requests: RegularizationRequestRepository,
        items: RegularizationItemRepository,
        actions: RegularizationActionRepository,
        employees: EmployeeRepository,
        schedule: ScheduleService,
        router: ApprovalRouter,
        users: UserRepository,
        access: AccessService,
        organizations: OrganizationRepository,
        settings: OrganizationSettingsRepository,
        audit: AuditWriter,
    ) -> None:
        self._engine = engine
        self._records = records
        self._punches = punches
        self._requests = requests
        self._items = items
        self._actions = actions
        self._employees = employees
        self._schedule = schedule
        self._router = router
        self._users = users
        self._access = access
        self._organizations = organizations
        self._settings = settings
        self._audit = audit

    # Helpers.

    def _settings_row(self, context: OrgContext) -> Any:
        row = self._settings.require(context.organization_id)
        if not row.enable_attendance_module:
            raise ModuleNotEnabled()
        return row

    def _today(self, context: OrgContext) -> tuple[ZoneInfo, date]:
        organization = self._organizations.get(context.organization_id)
        name = organization.timezone if organization else "UTC"
        return ZoneInfo(name), today_in(name)

    def _action(
        self,
        context: OrgContext,
        request: AttendanceRegularizationRequest,
        action: str,
        *,
        level: int | None = None,
        from_status: str | None = None,
        to_status: str | None = None,
        comment: str | None = None,
        **extra: Any,
    ) -> AttendanceRegularizationAction:
        row = AttendanceRegularizationAction(
            id=uuid7(),
            organization_id=context.organization_id,
            request_id=request.id,
            action=action,
            approval_level=level,
            from_status=from_status,
            to_status=to_status,
            comment=comment,
            actor_user_id=context.user_id,
            actor_employee_id=context.employee_id,
            occurred_at=utc_now(),
            created_by=context.user_id,
            **extra,
        )
        self._actions.add(row)
        self._actions.flush()
        return row

    @staticmethod
    def _route_to(
        request: AttendanceRegularizationRequest, level: int | None, route: Route | None
    ) -> None:
        request.current_approval_level = level
        request.current_approver_type = route.approver_type if route else None
        request.current_approver_user_id = route.user_id if route else None
        request.current_approver_employee_id = route.employee_id if route else None

    def _view(self, request: AttendanceRegularizationRequest) -> RequestView:
        return RequestView(
            request,
            list(self._items.for_request(request.id)),
            list(self._actions.for_request(request.id)),
        )

    def _lock(
        self, request_id: uuid.UUID
    ) -> tuple[AttendanceRegularizationRequest, AttendanceRecord]:
        """Employee, record, then request (7.3)."""
        request = self._requests.require(request_id, resource="regularization request")
        self._employees.get_for_update(request.employee_id)
        record = self._records.lock_find(request.employee_id, request.business_date)
        if record is None:  # pragma: no cover - the request references its record
            raise ResourceNotFound("attendance record")
        self._requests.session.refresh(request, with_for_update=True)
        return request, record

    # Submission (ATT-20).

    def submit(  # noqa: PLR0912 - one parameter per request column
        self,
        context: OrgContext,
        *,
        employee_id: uuid.UUID | None,
        on_behalf: bool,
        on_behalf_reason: str | None,
        day: date,
        request_type: str,
        reason: str,
        items: list[dict[str, Any]],
        supporting_document_id: uuid.UUID | None,
        resubmission_of: uuid.UUID | None,
    ) -> RequestView:
        settings = self._settings_row(context)
        if on_behalf:
            context.require(Permission.ATTENDANCE_PUNCH_MANAGE)
            if len((on_behalf_reason or "").strip()) < COMMENT_MIN:
                raise ValidationFailed("on_behalf_reason must be at least 10 characters.")
            target = self._employees.get(employee_id) if employee_id else None
            if target is None or not employee_in_scope(context, self._employees, target):
                raise ResourceNotFound("employee")
        else:
            context.require(Permission.SELF_ATTENDANCE)
            if not settings.enable_attendance_self_service:
                raise SelfServiceNotEnabled()
            if context.employee_id is None:
                raise PermissionDenied("This account is not linked to an employee record.")
            target = self._employees.require(context.employee_id, resource="employee")
        employee = self._employees.get_for_update(target.id)
        if employee is None:  # pragma: no cover - FK-backed
            raise ResourceNotFound("employee")
        organization_tz, today = self._today(context)
        if not attendance_eligible(
            self._requests.session,
            context.organization_id,
            settings.attendance_tracking_start_date,
            employee,
            day,
            today=today,
        ):
            raise EmployeeNotEligibleForAttendance()
        resolver = self._schedule.resolver(employee)
        schedule = resolver.at(day)
        if schedule.policy_version is None:
            raise NoApplicableAttendancePolicy()
        record = self._engine.get_or_create(employee.id, day, schedule, context.user_id)
        version = self._engine.policy_version(record)
        if not version.regularization_enabled:
            raise RegularizationNotEnabled()
        if day > today or day < today - timedelta(days=version.regularization_backdate_limit_days):
            raise RegularizationWindowExpired()
        if record.record_state != "OPEN":
            raise AttendanceRecordFinalized()
        if self._requests.pending_for_record(record.id) is not None:
            raise RegularizationAlreadyPending()
        self._validate_items(
            context,
            record,
            version.on_duty_requests_enabled,
            request_type,
            items,
            resolver,
            organization_tz,
            version.duplicate_punch_window_seconds,
        )

        route = self._router.route(
            employee,
            levels_for(version.regularization_approval_chain)[0],
            manager_permissions=_APPROVERS,
        )
        request = AttendanceRegularizationRequest(
            id=uuid7(),
            organization_id=context.organization_id,
            employee_id=employee.id,
            attendance_record_id=record.id,
            business_date=day,
            request_type=request_type,
            reason=reason,
            supporting_document_id=supporting_document_id,
            status="SUBMITTED",
            approval_chain_snapshot=version.regularization_approval_chain,
            submitted_by=context.user_id,
            submitted_at=utc_now(),
            submitted_on_behalf=on_behalf,
            resubmission_of_request_id=resubmission_of,
            created_by=context.user_id,
        )
        self._route_to(request, 1, route)
        self._requests.add(request)
        self._requests.flush()
        for item in items:
            self._items.add(
                AttendanceRegularizationItem(
                    id=uuid7(),
                    organization_id=context.organization_id,
                    request_id=request.id,
                    created_by=context.user_id,
                    **item,
                )
            )
        self._items.flush()
        record.has_pending_regularization = True
        self._action(
            context,
            request,
            "SUBMITTED",
            level=1,
            to_status="SUBMITTED",
            to_approver_type=route.approver_type,
            to_approver_user_id=route.user_id,
            comment="Routed to the HR queue: no eligible manager." if route.fell_back else None,
        )
        self._audit.record_change(
            context,
            action=AuditAction.ATTENDANCE_REGULARIZATION_SUBMITTED,
            entity_type="attendance_regularization_request",
            entity_id=request.id,
            employee_id=employee.id,
            after={"business_date": day, "request_type": request_type},
        )
        return self._view(request)

    def _validate_items(  # noqa: PLR0912, PLR0917
        self,
        context: OrgContext,
        record: AttendanceRecord,
        on_duty_enabled: bool,
        request_type: str,
        items: list[dict[str, Any]],
        resolver: Any,
        organization_tz: ZoneInfo,
        duplicate_seconds: int,
    ) -> None:
        kinds = [i["item_type"] for i in items]
        if not items or len(items) > MAX_ITEMS:
            raise RegularizationItemsInvalid("A request needs 1 to 10 items.")
        adds, voids, duties = (kinds.count(k) for k in ("ADD_PUNCH", "VOID_PUNCH", "SET_ON_DUTY"))
        if request_type == "MISSING_PUNCH" and not (adds >= 1 and voids == 0 and duties == 0):
            raise RegularizationItemsInvalid("MISSING_PUNCH needs ADD_PUNCH items only.")
        if request_type == "PUNCH_CORRECTION" and not (adds >= 1 and voids >= 1 and duties == 0):
            raise RegularizationItemsInvalid(
                "PUNCH_CORRECTION needs VOID_PUNCH and ADD_PUNCH items."
            )
        if request_type == "ON_DUTY":
            halves = [i["half"] for i in items if i["item_type"] == "SET_ON_DUTY"]
            if not on_duty_enabled:
                raise RegularizationNotEnabled("On-duty requests are not enabled by the policy.")
            if adds or voids or not 1 <= duties <= 2 or len(set(halves)) != duties:
                raise RegularizationItemsInvalid("ON_DUTY needs one or two SET_ON_DUTY halves.")
            for half in halves:
                if getattr(record, f"{half.lower()}_source") == "LEAVE":
                    raise AttendanceConflictsWithLeave()
        now = utc_now()
        window = timedelta(seconds=duplicate_seconds)
        for item in items:
            if item["item_type"] == "ADD_PUNCH":
                at: datetime = item["punch_at"]
                if at > now:
                    raise RegularizationItemsInvalid("An added punch cannot be in the future.")
                try:
                    located = assign_business_date(
                        at, organization_tz, lambda d: day_window(resolver.at(d), organization_tz)
                    )
                except NoApplicablePolicy as error:
                    raise NoApplicableAttendancePolicy() from error
                if located.business_date != record.business_date:
                    raise RegularizationItemsInvalid("An added punch resolves to another date.")
                if self._punches.nearby_valid(
                    record.employee_id, item["punch_type"], at - window, at + window
                ):
                    raise RegularizationItemsInvalid("An added punch duplicates an existing one.")
            elif item["item_type"] == "VOID_PUNCH":
                punch = self._punches.get(item["target_punch_id"])
                if (
                    punch is None
                    or punch.attendance_record_id != record.id
                    or punch.punch_status not in ("VALID", "OUT_OF_WINDOW")
                ):
                    raise RegularizationItemsInvalid(
                        "A void target must be a live punch of this record."
                    )

    # Decisions (7.1, ATT-21, ATT-22).

    def approve(
        self,
        context: OrgContext,
        request_id: uuid.UUID,
        *,
        comment: str | None,
        if_match: int | None,
    ) -> RequestView:
        self._settings_row(context)
        request, record = self._lock(request_id)
        assert_etag(request.row_version, if_match)
        if request.status != "SUBMITTED":
            raise InvalidApprovalTransition()
        override = authorize_decision(
            context,
            request,
            manager_permission=Permission.ATTENDANCE_REGULARIZE_APPROVE,
            hr_permission=Permission.ATTENDANCE_REGULARIZE_APPROVE_HR,
            comment=comment,
        )
        from_type = request.current_approver_type
        if (
            request.approval_chain_snapshot == "MANAGER_THEN_HR"
            and request.current_approval_level == 1
            and from_type == MANAGER
            and not override
        ):
            self._action(
                context,
                request,
                "APPROVED_LEVEL_1",
                level=1,
                from_status="SUBMITTED",
                to_status="SUBMITTED",
                from_approver_type=MANAGER,
                to_approver_type=HR_QUEUE,
                comment=comment,
            )
            self._route_to(request, 2, Route(HR_QUEUE, None, None))
            request.updated_by = context.user_id
            return self._view(request)
        if request.current_approval_level == 2:
            assert_distinct_approver(
                self._actions.actors(request.id, "APPROVED_LEVEL_1"), context.user_id
            )
        if record.record_state != "OPEN":
            raise AttendanceRecordFinalized()  # the request stays SUBMITTED (ATT-22)
        self._apply(context, request, record)
        self._action(
            context,
            request,
            "OVERRIDE_APPROVED" if override else "APPROVED_FINAL",
            level=request.current_approval_level,
            from_status="SUBMITTED",
            to_status="APPROVED",
            from_approver_type=from_type,
            comment=comment,
        )
        self._finish(context, request, record, "APPROVED")
        self._audit.record_change(
            context,
            action=AuditAction.ATTENDANCE_REGULARIZATION_APPROVED,
            entity_type="attendance_regularization_request",
            entity_id=request.id,
            employee_id=request.employee_id,
            after={"status": "APPROVED"},
        )
        return self._view(request)

    def _apply(
        self,
        context: OrgContext,
        request: AttendanceRegularizationRequest,
        record: AttendanceRecord,
    ) -> None:
        before = snapshot(record)
        touched: list[uuid.UUID] = []
        now = utc_now()
        for item in self._items.for_request(request.id):
            if item.item_type == "ADD_PUNCH":
                punch = AttendancePunch(
                    id=uuid7(),
                    organization_id=context.organization_id,
                    employee_id=record.employee_id,
                    punch_at=item.punch_at,
                    punch_type=item.punch_type,
                    source="REGULARIZATION",
                    work_arrangement=item.work_arrangement,
                    business_date=record.business_date,
                    attendance_record_id=record.id,
                    punch_status="VALID",
                    regularization_request_id=request.id,
                    created_by=context.user_id,
                )
                self._punches.add(punch)
                item.created_punch_id = punch.id
                touched.append(punch.id)
            elif item.item_type == "VOID_PUNCH":
                target = self._punches.get(item.target_punch_id) if item.target_punch_id else None
                if target is None or target.punch_status not in ("VALID", "OUT_OF_WINDOW"):
                    raise RegularizationStale()
                target.punch_status = "VOIDED"
                target.voided_at = now
                target.voided_by = context.user_id
                target.void_reason = "Voided by an approved regularization"
                target.void_source = "REGULARIZATION"
                touched.append(target.id)
            else:
                prefix = (item.half or "").lower()
                if getattr(record, f"{prefix}_source") == "LEAVE":
                    raise AttendanceConflictsWithLeave()
                setattr(record, f"{prefix}_status", "PRESENT")
                setattr(record, f"{prefix}_source", "ON_DUTY")
        self._punches.flush()
        self._engine.evaluate_into(record)
        self._engine.revise(
            record,
            before=before,
            change_source="REGULARIZATION_APPROVED",
            actor_user_id=context.user_id,
            punch_ids=touched,
            regularization_request_id=request.id,
        )

    def _finish(
        self,
        context: OrgContext,
        request: AttendanceRegularizationRequest,
        record: AttendanceRecord,
        status: str,
        comment: str | None = None,
    ) -> None:
        request.status = status
        request.decided_by = context.user_id
        request.decided_at = utc_now()
        request.decision_comment = comment
        request.updated_by = context.user_id
        self._route_to(request, None, None)
        record.has_pending_regularization = False  # ATT-23: the record itself is unchanged

    def reject(
        self,
        context: OrgContext,
        request_id: uuid.UUID,
        *,
        decision_comment: str,
        if_match: int | None,
    ) -> RequestView:
        self._settings_row(context)
        request, record = self._lock(request_id)
        assert_etag(request.row_version, if_match)
        if request.status != "SUBMITTED":
            raise InvalidApprovalTransition()
        comment = decision_comment.strip()
        if not COMMENT_MIN <= len(comment) <= 1000:
            raise ValidationFailed("decision_comment must be 10 to 1,000 characters.")
        authorize_decision(
            context,
            request,
            manager_permission=Permission.ATTENDANCE_REGULARIZE_APPROVE,
            hr_permission=Permission.ATTENDANCE_REGULARIZE_APPROVE_HR,
            comment=comment,
        )
        self._action(
            context,
            request,
            "REJECTED",
            level=request.current_approval_level,
            from_status="SUBMITTED",
            to_status="REJECTED",
            comment=comment,
        )
        self._finish(context, request, record, "REJECTED", comment)
        self._audit.record_change(
            context,
            action=AuditAction.ATTENDANCE_REGULARIZATION_REJECTED,
            entity_type="attendance_regularization_request",
            entity_id=request.id,
            employee_id=request.employee_id,
            reason=comment,
        )
        return self._view(request)

    def withdraw(
        self,
        context: OrgContext,
        request_id: uuid.UUID,
        *,
        reason: str | None,
        if_match: int | None,
    ) -> RequestView:
        self._settings_row(context)
        request, record = self._lock(request_id)
        owner = context.is_self(request.employee_id) and context.has(Permission.SELF_ATTENDANCE)
        if not (owner or context.has(Permission.ATTENDANCE_PUNCH_MANAGE)):
            raise ResourceNotFound("regularization request")
        assert_etag(request.row_version, if_match)
        if request.status != "SUBMITTED":
            raise InvalidApprovalTransition()
        self._action(
            context,
            request,
            "WITHDRAWN",
            from_status="SUBMITTED",
            to_status="WITHDRAWN",
            comment=reason,
        )
        self._finish(context, request, record, "WITHDRAWN")
        self._audit.record_change(
            context,
            action=AuditAction.ATTENDANCE_REGULARIZATION_WITHDRAWN,
            entity_type="attendance_regularization_request",
            entity_id=request.id,
            employee_id=request.employee_id,
            reason=reason,
        )
        return self._view(request)

    def reassign(
        self,
        context: OrgContext,
        request_id: uuid.UUID,
        *,
        to_user_id: uuid.UUID | None,
        to_hr_queue: bool,
        reason: str,
        if_match: int | None,
    ) -> RequestView:
        """INT-35 for regularizations."""
        self._settings_row(context)
        context.require(Permission.ATTENDANCE_REGULARIZE_APPROVE_HR)
        request, _record = self._lock(request_id)
        assert_etag(request.row_version, if_match)
        if request.status != "SUBMITTED":
            raise InvalidApprovalTransition()
        if (to_user_id is None) == (not to_hr_queue):
            raise ValidationFailed("Provide exactly one of to_user_id or to_hr_queue.")
        if len(reason.strip()) < COMMENT_MIN:
            raise ValidationFailed("A reason of at least 10 characters is required.")
        route = Route(HR_QUEUE, None, None)
        if to_user_id is not None:
            user = self._users.get(to_user_id)
            if user is None or not user.is_active:
                raise ResourceNotFound("user")
            if to_user_id == request.submitted_by or user.employee_id == request.employee_id:
                raise SelfApprovalNotAllowed()
            if not self._access.resolve(to_user_id, context.organization_id).permissions.has_any(
                *_APPROVERS
            ):
                raise NotCurrentApprover("The target user cannot approve regularizations.")
            route = Route(MANAGER, to_user_id, user.employee_id)
        previous = (request.current_approver_type, request.current_approver_user_id)
        self._route_to(request, request.current_approval_level or 1, route)
        self._action(
            context,
            request,
            "REASSIGNED",
            level=request.current_approval_level,
            from_approver_type=previous[0],
            from_approver_user_id=previous[1],
            to_approver_type=route.approver_type,
            to_approver_user_id=route.user_id,
            comment=reason,
        )
        request.updated_by = context.user_id
        self._audit.record_change(
            context,
            action=AuditAction.ATTENDANCE_REGULARIZATION_REASSIGNED,
            entity_type="attendance_regularization_request",
            entity_id=request.id,
            employee_id=request.employee_id,
            reason=reason,
        )
        return self._view(request)

    # Reads.

    def get(self, context: OrgContext, request_id: uuid.UUID) -> RequestView:
        request = self._requests.require(request_id, resource="regularization request")
        employee = self._employees.get(request.employee_id)
        own = context.is_self(request.employee_id) and context.has(Permission.SELF_ATTENDANCE)
        approver = request.current_approver_user_id == context.user_id or context.user_id in {
            a.actor_user_id for a in self._actions.for_request(request.id)
        }
        scoped = (
            context.has(Permission.ATTENDANCE_VIEW)
            and employee is not None
            and employee_in_scope(context, self._employees, employee)
        )
        if not (own or approver or scoped):
            raise ResourceNotFound("regularization request")
        return self._view(request)

    def list_requests(
        self,
        context: OrgContext,
        *,
        employee_id: uuid.UUID | None,
        statuses: list[str] | None,
        approver_me: bool,
        hr_queue: bool,
        own: bool = False,
    ) -> list[AttendanceRegularizationRequest]:
        employee_ids: list[uuid.UUID] | None = None
        approver_user_id: uuid.UUID | None = None
        queue = False
        if own:
            context.require(Permission.SELF_ATTENDANCE)
            employee_id = context.employee_id
        elif approver_me or hr_queue:
            if hr_queue:
                context.require(Permission.ATTENDANCE_REGULARIZE_APPROVE_HR)
                queue = True
            if approver_me:
                context.require_any(*_APPROVERS)
                approver_user_id = context.user_id
        else:
            context.require(Permission.ATTENDANCE_VIEW)
            if context.is_reporting_scoped:
                employee_ids = [
                    *self._employees.descendant_ids(context.employee_id or uuid.uuid4())
                ]
                if context.employee_id:
                    employee_ids.append(context.employee_id)
        return list(
            self._requests.listing(
                employee_id=employee_id,
                employee_ids=employee_ids,
                statuses=statuses,
                approver_user_id=approver_user_id,
                hr_queue=queue,
            )
        )

    # Lifecycle side effects (7.5, INT-36); the caller holds the employee row lock.

    def reroute_for_manager_change(self, context: OrgContext, employee: Any) -> int:
        """INT-36: SUBMITTED requests at a MANAGER level follow the employee's new manager."""
        if not self._settings.require(context.organization_id).enable_attendance_module:
            return 0
        changed = 0
        for listed in self._requests.listing(
            employee_id=employee.id,
            employee_ids=None,
            statuses=("SUBMITTED",),
            approver_user_id=None,
            hr_queue=False,
        ):
            if listed.current_approver_type != MANAGER:
                continue
            request = self._requests.require(listed.id, resource="regularization request")
            self._requests.session.refresh(request, with_for_update=True)
            route = self._router.route(employee, MANAGER, manager_permissions=_APPROVERS)
            previous = (request.current_approver_type, request.current_approver_user_id)
            self._route_to(request, request.current_approval_level or 1, route)
            self._action(
                context,
                request,
                "REASSIGNED",
                level=request.current_approval_level,
                from_approver_type=previous[0],
                from_approver_user_id=previous[1],
                to_approver_type=route.approver_type,
                to_approver_user_id=route.user_id,
                comment="MANAGER_CHANGE",
            )
            request.updated_by = context.user_id
            changed += 1
        return changed

    def system_cancel_for_employee(
        self, context: OrgContext, employee: Any, *, reason_code: str, end_date: date | None
    ) -> int:
        """7.5: SUBMITTED requests for dates after the end date (all on archive) are CANCELLED.

        The record itself is never changed (ATT-23); only the pending flag is cleared.
        """
        if not self._settings.require(context.organization_id).enable_attendance_module:
            return 0
        changed = 0
        for listed in self._requests.listing(
            employee_id=employee.id,
            employee_ids=None,
            statuses=("SUBMITTED",),
            approver_user_id=None,
            hr_queue=False,
        ):
            if end_date is not None and listed.business_date <= end_date:
                continue
            request, record = self._lock(listed.id)
            self._action(
                context,
                request,
                "CANCELLED",
                from_status="SUBMITTED",
                to_status="CANCELLED",
                comment=reason_code,
            )
            request.cancellation_reason = reason_code
            self._finish(context, request, record, "CANCELLED")
            self._audit.record_change(
                context,
                action=AuditAction.ATTENDANCE_REGULARIZATION_CANCELLED,
                entity_type="attendance_regularization_request",
                entity_id=request.id,
                employee_id=employee.id,
                reason=reason_code,
            )
            changed += 1
        return changed
