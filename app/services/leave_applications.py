"""Leave applications: validation, submission, approval and cancellation (addendum §6.8-§6.12).

Lock order (§7.3) in every mutating use case: the employee row, then the ``leave_balances``
row(s) in ascending (type, year), then the application. Nothing is posted to the ledger except
through :class:`~app.services.leave_ledger.LeaveLedger`, keyed so a retry is a no-op.

Attendance integration (INT-10..13, LEV-30 punch conflict, LEV-31) is not wired: the attendance
record tables do not exist yet. Those hooks are marked ``shortcut:`` where they belong.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, or_
from sqlalchemy.exc import IntegrityError

from app.audit.actions import AuditAction
from app.audit.writer import AuditWriter
from app.core.clock import today_in, utc_now
from app.core.context import OrgContext
from app.core.errors import (
    EmployeeNotEligibleForLeave,
    HalfDayNotAllowed,
    InsufficientLeaveBalance,
    InvalidApprovalTransition,
    InvalidLeaveDateRange,
    LeaveApplicationLimitExceeded,
    LeaveBoundaryOnNonWorkingDay,
    LeaveConsecutiveLimitExceeded,
    LeaveDocumentRequired,
    LeaveDurationLimit,
    LeaveNotCancellable,
    LeaveOverlap,
    LeaveSpansLeaveYears,
    LeaveTimingViolation,
    LeaveTypeNotApplicable,
    LeaveZeroDuration,
    ModuleNotEnabled,
    NotCurrentApprover,
    OptionalHolidayInvalid,
    PermissionDenied,
    ResourceNotFound,
    RestorationModeRequired,
    SelfApprovalNotAllowed,
    SelfServiceNotEnabled,
    ValidationFailed,
    WarningDetail,
)
from app.core.permissions import Permission
from app.core.warnings import WarningCode, WarningCollector
from app.db.types import uuid7
from app.models.employee import Employee, EmployeeStatus
from app.models.leave import (
    ApplicationAction,
    ApplicationStatus,
    LeaveApplication,
    LeaveApplicationAction,
    LeaveApplicationSlot,
    LeaveBalance,
    LeavePolicyVersion,
    LeaveType,
    TransactionType,
)
from app.repositories.employee import EmployeeRepository
from app.repositories.leave import (
    LeaveActionRepository,
    LeaveApplicationRepository,
    LeaveBalanceRepository,
    LeaveSlotRepository,
    LeaveTypeRepository,
)
from app.repositories.organization import (
    OrganizationRepository,
    OrganizationSettingsRepository,
)
from app.repositories.platform import UserRepository
from app.repositories.schedule import HolidayRepository
from app.schemas.common import CursorCodec, SortDirection
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
from app.services.attendance_records import AttendanceLeaveEffects
from app.services.leave_config import LeaveConfigService
from app.services.leave_ledger import LeaveLedger, Posting
from app.services.leave_rules import (
    Half,
    SlotResult,
    evaluate_slots,
    leave_year_start,
    next_leave_year_start,
)
from app.services.organizations import assert_etag
from app.services.schedule import ScheduleService

HALF_DAY = Decimal("0.50")
ONE_DAY = Decimal("1.00")
CANCEL_REASON_MIN = 10
DECISION_COMMENT_MIN = 10
_LEAVE_APPROVER_PERMISSIONS = (Permission.LEAVE_APPROVE, Permission.LEAVE_APPROVE_HR)
_SLOT_UNIQUE = "uq_leave_application_slots_active"
_TERMINAL = {
    ApplicationStatus.REJECTED.value,
    ApplicationStatus.WITHDRAWN.value,
    ApplicationStatus.CANCELLED.value,
}


@dataclass(slots=True)
class Evaluation:
    """Outcome of LEV-20..LEV-31 for one prospective application."""

    employee: Employee
    leave_type: LeaveType
    version: LeavePolicyVersion
    year_start: date
    slots: list[SlotResult]
    requested_days: Decimal
    available_before: Decimal | None
    collector: WarningCollector = field(default_factory=WarningCollector)
    holiday_id: uuid.UUID | None = None

    @property
    def warnings(self) -> list[WarningDetail]:
        return self.collector.raised


@dataclass(frozen=True, slots=True)
class ApplicationView:
    application: LeaveApplication
    slots: list[LeaveApplicationSlot]
    actions: list[LeaveApplicationAction]
    balance: LeaveBalance | None


class LeaveApplicationService:
    def __init__(
        self,
        *,
        applications: LeaveApplicationRepository,
        slots: LeaveSlotRepository,
        actions: LeaveActionRepository,
        ledger: LeaveLedger,
        balances: LeaveBalanceRepository,
        config: LeaveConfigService,
        types: LeaveTypeRepository,
        employees: EmployeeRepository,
        holidays: HolidayRepository,
        schedule: ScheduleService,
        router: ApprovalRouter,
        organizations: OrganizationRepository,
        settings: OrganizationSettingsRepository,
        users: UserRepository,
        access: AccessService,
        audit: AuditWriter,
        codec: CursorCodec,
        attendance: AttendanceLeaveEffects | None = None,
    ) -> None:
        self._attendance = attendance
        self._codec = codec
        self._applications = applications
        self._slots = slots
        self._actions = actions
        self._ledger = ledger
        self._balances = balances
        self._config = config
        self._types = types
        self._employees = employees
        self._holidays = holidays
        self._schedule = schedule
        self._router = router
        self._organizations = organizations
        self._settings = settings
        self._users = users
        self._access = access
        self._audit = audit

    # ------------------------------------------------------------------ guards

    def _effects(self, context: OrgContext) -> AttendanceLeaveEffects | None:
        """The attendance collaborator, or None while the attendance module is off."""
        if self._attendance is None or not self._settings_row(context).enable_attendance_module:
            return None
        return self._attendance

    def leave_type(self, type_id: uuid.UUID) -> LeaveType:
        return self._types.require(type_id, resource="leave type")

    def _today(self, context: OrgContext) -> date:
        organization = self._organizations.get(context.organization_id)
        return today_in(organization.timezone if organization else None)

    def _settings_row(self, context: OrgContext) -> Any:
        return self._settings.require(context.organization_id)

    def _require_module(self, context: OrgContext) -> None:
        if not self._settings_row(context).enable_leave_module:
            raise ModuleNotEnabled()

    def own_employee_id(self, context: OrgContext) -> uuid.UUID:
        """§11: self-service identity comes from the user link and the SELF grant, never a path."""
        context.require(Permission.SELF_LEAVE)
        settings = self._settings_row(context)
        if not (settings.enable_leave_module and settings.enable_leave_self_service):
            raise SelfServiceNotEnabled()
        if context.employee_id is None:
            raise PermissionDenied("This account is not linked to an employee record.")
        return context.employee_id

    # ------------------------------------------------------------------ evaluation

    def _day_type_fn(self, employee_id: uuid.UUID) -> Any:
        """One cached resolver per call site, so a range costs one load instead of one per day."""
        employee = self._employees.require(employee_id, resource="employee")
        return self._schedule.resolver(employee).day_type

    def evaluate(  # noqa: PLR0912, PLR0915 - one linear pass over the LEV-20..LEV-31 table
        self,
        context: OrgContext,
        employee: Employee,
        body: dict[str, Any],
        *,
        on_behalf: bool,
        acknowledged: list[str],
        exclude_application_id: uuid.UUID | None = None,
        balance: LeaveBalance | None = None,
    ) -> Evaluation:
        self._require_module(context)
        today = self._today(context)
        settings = self._settings_row(context)
        start, end = body["start_date"], body["end_date"]
        start_half, end_half = Half(body["start_half"]), Half(body["end_half"])

        leave_type = self._types.require(body["leave_type_id"], resource="leave type")
        if leave_type.status != "ACTIVE":
            raise LeaveTypeNotApplicable()
        version = self._config.resolve_for(employee, leave_type.id, start)

        # LEV-21
        if employee.archived_at is not None or employee.status not in (
            EmployeeStatus.ACTIVE.value,
            EmployeeStatus.ON_NOTICE.value if version.allowed_during_notice else "",
        ):
            raise EmployeeNotEligibleForLeave()
        # LEV-22
        joining = employee.joining_date
        if joining is None or start < joining + timedelta(days=version.min_service_days or 0):
            raise EmployeeNotEligibleForLeave("Leave starts before the minimum service period.")
        if employee.employment_end_date is not None and end > employee.employment_end_date:
            raise EmployeeNotEligibleForLeave("Leave extends beyond the employment end date.")
        tracking = settings.leave_tracking_start_date
        if tracking is not None and start < tracking:
            raise EmployeeNotEligibleForLeave("Leave starts before leave tracking began.")
        # LEV-23
        in_probation = (
            employee.probation_end_date is not None
            and start <= employee.probation_end_date
            and (employee.confirmation_date is None or start < employee.confirmation_date)
        )
        if in_probation and not version.allowed_during_probation:
            raise EmployeeNotEligibleForLeave("Leave is not allowed during probation.")

        # LEV-24 range
        if end < start or (start == end and start_half > end_half):
            raise InvalidLeaveDateRange()
        year_start = leave_year_start(start, settings.leave_year_start_month)
        if leave_year_start(end, settings.leave_year_start_month) != year_start:
            raise LeaveSpansLeaveYears()
        if not version.allow_half_day and (
            start_half != Half.FIRST_HALF or end_half != Half.SECOND_HALF
        ):
            raise HalfDayNotAllowed()

        holiday_id: uuid.UUID | None = None
        if leave_type.leave_category == "OPTIONAL_HOLIDAY":
            holiday_id = self._assert_optional_holiday(
                employee, body, start, end, start_half, end_half
            )

        day_type_of = self._day_type_fn(employee.id)
        slots, requested = evaluate_slots(
            start,
            start_half,
            end,
            end_half,
            day_type_of,
            count_weekly_offs=version.count_weekly_offs,
            count_holidays=version.count_holidays,
        )
        if holiday_id is not None:
            slots = [SlotResult(s.slot_date, s.half, "WORKING_DAY", True) for s in slots]
            requested = ONE_DAY
        else:
            for boundary in (start, end):
                kind = day_type_of(boundary)
                if not _counts(kind, version):
                    raise LeaveBoundaryOnNonWorkingDay()
        if requested <= 0:
            raise LeaveZeroDuration()
        if (version.min_days_per_application and requested < version.min_days_per_application) or (
            version.max_days_per_application and requested > version.max_days_per_application
        ):
            raise LeaveDurationLimit()
        if version.max_consecutive_days is not None:
            self._assert_chain(
                employee.id, leave_type.id, start, end, requested, version, day_type_of
            )

        # LEV-25
        bypass_notice = on_behalf and context.has(Permission.LEAVE_APPLY_ON_BEHALF)
        if start < today - timedelta(days=version.max_backdate_days) or end > today + timedelta(
            days=version.max_future_days
        ):
            raise LeaveTimingViolation()
        if (
            version.min_notice_days
            and not bypass_notice
            and start < today + timedelta(days=version.min_notice_days)
        ):
            raise LeaveTimingViolation("The application does not meet the minimum notice period.")

        # LEV-26
        if any(
            (s.slot_date, s.half) in {(x.slot_date, x.half) for x in slots}
            for s in self._slots.active_in_range(employee.id, start, end)
            if s.application_id != exclude_application_id
        ):
            raise LeaveOverlap()

        # LEV-27
        threshold = version.document_required_above_days
        if (
            threshold is not None
            and requested > threshold
            and not body.get("supporting_document_id")
        ):
            raise LeaveDocumentRequired()
        if (
            version.max_applications_per_year is not None
            and self._applications.submitted_count_in_year(employee.id, leave_type.id, year_start)
            >= version.max_applications_per_year
        ):
            raise LeaveApplicationLimitExceeded()

        # LEV-29 / LEV-30 (balance part)
        collector = WarningCollector(acknowledged)
        available: Decimal | None = None
        if leave_type.tracks_balance:
            row = balance or self._balances.find(employee.id, leave_type.id, year_start)
            available = row.available_days if row is not None else Decimal("0.00")
            if exclude_application_id is not None:
                available += requested  # own reservation excluded (final-approval check)
            allowed_negative = _allowed_negative(version)
            if available - requested < -allowed_negative:
                raise InsufficientLeaveBalance(
                    f"Available {available}, requested {requested}, "
                    f"allowed negative {allowed_negative}."
                )
            collector.add_if(available - requested < 0, WarningCode.LEAVE_NEGATIVE_BALANCE)
        effects = self._effects(context)
        if effects is not None:
            effects.assert_no_override_conflict(employee.id, slots)  # LEV-31
            collector.add_if(
                effects.punch_conflict(employee.id, slots), WarningCode.LEAVE_PUNCH_CONFLICT
            )  # LEV-30

        return Evaluation(
            employee=employee,
            leave_type=leave_type,
            version=version,
            year_start=year_start,
            slots=slots,
            requested_days=requested,
            available_before=available,
            collector=collector,
            holiday_id=holiday_id,
        )

    def _assert_optional_holiday(  # noqa: PLR0917
        self,
        employee: Employee,
        body: dict[str, Any],
        start: date,
        end: date,
        start_half: Half,
        end_half: Half,
    ) -> uuid.UUID:
        holiday = self._holidays.get(body["holiday_id"]) if body.get("holiday_id") else None
        calendar_id = self._schedule.calendar_id_for(employee.id, start)
        if (
            holiday is None
            or start != end
            or start_half != Half.FIRST_HALF
            or end_half != Half.SECOND_HALF
            or holiday.status != "ACTIVE"
            or holiday.holiday_type != "OPTIONAL"
            or holiday.holiday_date != start
            or holiday.calendar_id != calendar_id
        ):
            raise OptionalHolidayInvalid()
        return holiday.id

    def _assert_chain(  # noqa: PLR0917
        self,
        employee_id: uuid.UUID,
        type_id: uuid.UUID,
        start: date,
        end: date,
        requested: Decimal,
        version: LeavePolicyVersion,
        day_type_of: Any,
    ) -> None:
        """LEV-24: this application plus adjacent pending/approved ones, across non-working gaps."""
        chain_start, chain_end, total = start, end, requested
        pending = list(self._applications.adjacent_chain_days(employee_id, type_id))
        grew = True
        while grew:
            grew = False
            for other in list(pending):
                before = other.end_date < chain_start and _all_non_working(
                    other.end_date + timedelta(days=1), chain_start - timedelta(days=1), day_type_of
                )
                after = other.start_date > chain_end and _all_non_working(
                    chain_end + timedelta(days=1), other.start_date - timedelta(days=1), day_type_of
                )
                overlapping = other.start_date <= chain_end and other.end_date >= chain_start
                if before or after or overlapping:
                    chain_start = min(chain_start, other.start_date)
                    chain_end = max(chain_end, other.end_date)
                    total += other.requested_days
                    pending.remove(other)
                    grew = True
        if version.max_consecutive_days is not None and total > version.max_consecutive_days:
            raise LeaveConsecutiveLimitExceeded()

    # ------------------------------------------------------------------ helpers

    def _target_employee(
        self, context: OrgContext, employee_id: uuid.UUID | None, *, on_behalf: bool
    ) -> Employee:
        if on_behalf:
            context.require(Permission.LEAVE_APPLY_ON_BEHALF)
            employee = self._employees.get(employee_id) if employee_id else None
            if employee is None or not employee_in_scope(context, self._employees, employee):
                raise ResourceNotFound("employee")
            return employee
        employee = self._employees.get(self.own_employee_id(context))
        if employee is None:  # pragma: no cover - the user link is FK-backed
            raise ResourceNotFound("employee")
        return employee

    def _record_action(
        self,
        context: OrgContext,
        application: LeaveApplication,
        action: ApplicationAction,
        *,
        level: int | None = None,
        from_status: str | None = None,
        to_status: str | None = None,
        comment: str | None = None,
        **extra: Any,
    ) -> LeaveApplicationAction:
        row = LeaveApplicationAction(
            id=uuid7(),
            organization_id=context.organization_id,
            application_id=application.id,
            action=action.value,
            approval_level=level,
            from_status=from_status,
            to_status=to_status,
            requested_days_after=application.requested_days,
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

    def _audit_application(
        self,
        context: OrgContext,
        application: LeaveApplication,
        action: AuditAction,
        *,
        before: dict[str, Any] | None = None,
        after: dict[str, Any] | None = None,
        reason: str | None = None,
        acknowledged: list[str] | None = None,
    ) -> None:
        self._audit.record_change(
            context,
            action=action,
            entity_type="leave_application",
            entity_id=application.id,
            employee_id=application.employee_id,
            before=before,
            after=after or {"status": application.status},
            reason=reason,
            acknowledged_warnings=acknowledged,
        )

    def _lock_for_mutation(
        self, application_id: uuid.UUID
    ) -> tuple[Employee, LeaveBalance | None, LeaveApplication, LeaveType]:
        """§7.3 order: employee row, then the balance row, then the application."""
        application = self._applications.require(application_id, resource="leave application")
        employee = self._employees.get_for_update(application.employee_id)
        leave_type = self._types.require(application.leave_type_id, resource="leave type")
        if employee is None:  # pragma: no cover - FK-backed
            raise ResourceNotFound("employee")
        balance = (
            self._ledger.lock_balance(employee.id, leave_type.id, application.leave_year_start)
            if leave_type.tracks_balance
            else None
        )
        self._applications.session.refresh(application, with_for_update=True)
        return employee, balance, application, leave_type

    def _release(
        self, application: LeaveApplication, balance: LeaveBalance | None, slot_status: str
    ) -> None:
        if balance is not None and application.reserves_balance:
            balance.reserved_days = balance.reserved_days - application.requested_days
        self._slots.release(application.id, slot_status)

    @staticmethod
    def _clear_routing(application: LeaveApplication) -> None:
        application.current_approval_level = None
        application.current_approver_type = None
        application.current_approver_user_id = None
        application.current_approver_employee_id = None

    @staticmethod
    def _apply_route(application: LeaveApplication, level: int, route: Route) -> None:
        application.current_approval_level = level
        application.current_approver_type = route.approver_type
        application.current_approver_user_id = route.user_id
        application.current_approver_employee_id = route.employee_id

    def _fresh_balance(self, application: LeaveApplication) -> LeaveBalance | None:
        balance = self._balances.find(
            application.employee_id, application.leave_type_id, application.leave_year_start
        )
        return self._balances.reload(balance) if balance is not None else None

    def _view(self, application: LeaveApplication) -> ApplicationView:
        return ApplicationView(
            application=application,
            slots=list(self._slots.for_application(application.id)),
            actions=list(self._actions.for_application(application.id)),
            balance=self._fresh_balance(application),
        )

    # ------------------------------------------------------------------ preview / submit

    def preview(
        self,
        context: OrgContext,
        *,
        employee_id: uuid.UUID | None,
        body: dict[str, Any],
        acknowledged: list[str],
    ) -> Evaluation:
        """Dry run: same rules as submission, no locks and no writes (§6.15)."""
        on_behalf = employee_id is not None and not context.is_self(employee_id)
        employee = self._target_employee(context, employee_id, on_behalf=on_behalf)
        return self.evaluate(
            context, employee, body, on_behalf=on_behalf, acknowledged=acknowledged
        )

    def submit(
        self,
        context: OrgContext,
        *,
        employee_id: uuid.UUID | None,
        body: dict[str, Any],
        on_behalf: bool,
        acknowledged: list[str],
    ) -> ApplicationView:
        self._require_module(context)
        employee = self._target_employee(context, employee_id, on_behalf=on_behalf)
        reason_text = (body.get("on_behalf_reason") or "").strip()
        if on_behalf and len(reason_text) < 10:
            raise ValidationFailed("on_behalf_reason must be at least 10 characters.").with_detail(
                field="on_behalf_reason", code="TOO_SHORT", message="Explain why."
            )
        locked = self._employees.get_for_update(employee.id)  # lock 1: employee
        if locked is None:  # pragma: no cover - FK-backed
            raise ResourceNotFound("employee")
        leave_type = self._types.require(body["leave_type_id"], resource="leave type")
        year_start = leave_year_start(
            body["start_date"], self._settings_row(context).leave_year_start_month
        )
        balance = (  # lock 2: the balance row
            self._ledger.lock_balance(locked.id, leave_type.id, year_start)
            if leave_type.tracks_balance
            else None
        )
        evaluation = self.evaluate(
            context, locked, body, on_behalf=on_behalf, acknowledged=acknowledged, balance=balance
        )
        acknowledged_codes = evaluation.collector.enforce()
        version = evaluation.version

        chain = levels_for(version.approval_chain)
        route = self._router.route(
            locked, chain[0], manager_permissions=_LEAVE_APPROVER_PERMISSIONS
        )
        application = LeaveApplication(
            id=uuid7(),
            organization_id=context.organization_id,
            employee_id=locked.id,
            leave_type_id=leave_type.id,
            leave_type_code_snapshot=leave_type.leave_type_code,
            policy_version_id=version.id,
            leave_year_start=evaluation.year_start,
            start_date=body["start_date"],
            start_half=body["start_half"],
            end_date=body["end_date"],
            end_half=body["end_half"],
            holiday_id=evaluation.holiday_id,
            requested_days=evaluation.requested_days,
            reason=body.get("reason"),
            supporting_document_id=body.get("supporting_document_id"),
            status=ApplicationStatus.SUBMITTED.value,
            reserves_balance=leave_type.tracks_balance and version.reserve_pending_applications,
            approval_chain_snapshot=version.approval_chain,
            submitted_by=context.user_id,
            submitted_at=utc_now(),
            submitted_on_behalf=on_behalf,
            on_behalf_reason=reason_text or None,
            consumed_days=Decimal("0.00"),
            cancelled_days=Decimal("0.00"),
            resubmission_of_application_id=body.get("resubmission_of_application_id"),
            created_by=context.user_id,
        )
        self._apply_route(application, 1, route)
        self._applications.add(application)
        self._applications.flush()
        self._slots.add_all(
            [
                LeaveApplicationSlot(
                    id=uuid7(),
                    organization_id=context.organization_id,
                    employee_id=locked.id,
                    application_id=application.id,
                    slot_date=slot.slot_date,
                    half=slot.half.value,
                    day_type=slot.day_type,
                    counted=slot.counted,
                    created_by=context.user_id,
                )
                for slot in evaluation.slots
            ]
        )
        try:
            self._slots.flush()
        except IntegrityError as exc:
            if _SLOT_UNIQUE not in str(exc.orig):
                raise
            raise LeaveOverlap() from exc  # the DB-level overlap guarantee (LEV-26)
        if balance is not None and application.reserves_balance:
            balance.reserved_days = balance.reserved_days + application.requested_days
        self._record_action(
            context,
            application,
            ApplicationAction.SUBMITTED,
            level=1,
            to_status=ApplicationStatus.SUBMITTED.value,
            to_approver_type=route.approver_type,
            to_approver_user_id=route.user_id,
            comment="Routed to the HR queue: no eligible manager." if route.fell_back else None,
        )
        self._audit_application(
            context,
            application,
            AuditAction.LEAVE_APPLICATION_SUBMITTED,
            after={
                "leave_type": leave_type.leave_type_code,
                "start_date": application.start_date,
                "end_date": application.end_date,
                "requested_days": str(application.requested_days),
            },
            acknowledged=acknowledged_codes,
        )
        return self._view(application)

    # ------------------------------------------------------------------ decisions

    def approve(
        self,
        context: OrgContext,
        application_id: uuid.UUID,
        *,
        comment: str | None,
        if_match: int | None,
        acknowledged: list[str],
    ) -> ApplicationView:
        self._require_module(context)
        employee, balance, application, leave_type = self._lock_for_mutation(application_id)
        self._assert_decidable(application, if_match)
        override = authorize_decision(
            context,
            application,
            manager_permission=Permission.LEAVE_APPROVE,
            hr_permission=Permission.LEAVE_APPROVE_HR,
            comment=comment,
        )
        from_type = application.current_approver_type
        if (
            application.approval_chain_snapshot == "MANAGER_THEN_HR"
            and application.current_approval_level == 1
            and from_type == MANAGER
            and not override
        ):
            action = self._record_action(
                context,
                application,
                ApplicationAction.APPROVED_LEVEL_1,
                level=1,
                from_status=ApplicationStatus.SUBMITTED.value,
                to_status=ApplicationStatus.SUBMITTED.value,
                from_approver_type=MANAGER,
                to_approver_type=HR_QUEUE,
                comment=comment,
            )
            self._apply_route(application, 2, Route(HR_QUEUE, None, None))
            application.updated_by = context.user_id
            self._audit_application(
                context,
                application,
                AuditAction.LEAVE_APPLICATION_APPROVED,
                after={"status": application.status, "level": 1, "action_id": str(action.id)},
            )
            return self._view(application)

        if application.current_approval_level == 2:
            assert_distinct_approver(
                self._actions.actors(application.id, ApplicationAction.APPROVED_LEVEL_1.value),
                context.user_id,
            )
        acknowledged_codes = self._finalise_approval(
            context, employee, balance, application, acknowledged
        )
        action = self._record_action(
            context,
            application,
            ApplicationAction.OVERRIDE_APPROVED if override else ApplicationAction.APPROVED_FINAL,
            level=application.current_approval_level,
            from_status=ApplicationStatus.SUBMITTED.value,
            to_status=ApplicationStatus.APPROVED.value,
            from_approver_type=from_type,
            comment=comment,
        )
        if balance is not None:
            self._ledger.post(
                balance,
                Posting(
                    employee_id=employee.id,
                    leave_type_id=leave_type.id,
                    year_start=application.leave_year_start,
                    transaction_type=TransactionType.CONSUMPTION.value,
                    quantity_days=-application.requested_days,
                    effective_date=application.start_date,
                    idempotency_key=f"CON:{application.id}:{action.id}",
                    actor_id=context.user_id,
                    leave_application_id=application.id,
                    application_action_id=action.id,
                ),
            )
            if application.reserves_balance:
                balance.reserved_days = balance.reserved_days - application.requested_days
        application.status = ApplicationStatus.APPROVED.value
        application.consumed_days = application.requested_days
        application.decided_by = context.user_id
        application.decided_at = utc_now()
        application.updated_by = context.user_id
        self._clear_routing(application)
        self._audit_application(
            context,
            application,
            AuditAction.LEAVE_APPLICATION_APPROVED,
            acknowledged=acknowledged_codes,
        )
        effects = self._effects(context)
        if effects is not None:  # INT-10: counted halves of existing records become ON_LEAVE
            effects.on_approved(
                employee.id,
                application,
                self._slots.for_application(application.id, status="ACTIVE"),
                context.user_id,
            )
        return self._view(application)

    @staticmethod
    def _assert_decidable(application: LeaveApplication, if_match: int | None) -> None:
        assert_etag(application.row_version, if_match)
        if application.status != ApplicationStatus.SUBMITTED.value:
            raise InvalidApprovalTransition()

    def _finalise_approval(
        self,
        context: OrgContext,
        employee: Employee,
        balance: LeaveBalance | None,
        application: LeaveApplication,
        acknowledged: list[str],
    ) -> list[str]:
        """Re-check LEV-21 / LEV-29 and recompute the duration with the current schedule."""
        version = self._config.version_by_id(application.policy_version_id)
        if employee.archived_at is not None or employee.status not in (
            EmployeeStatus.ACTIVE.value,
            EmployeeStatus.ON_NOTICE.value if version.allowed_during_notice else "",
        ):
            raise EmployeeNotEligibleForLeave()
        if application.holiday_id is None:
            self._recalculate_pending(context, application, balance, version)
        collector = WarningCollector(acknowledged)
        if balance is not None:
            # The application's own reservation is excluded from the availability check.
            available = balance.available_days + (
                application.requested_days if application.reserves_balance else Decimal(0)
            )
            allowed = _allowed_negative(version)
            if available - application.requested_days < -allowed:
                raise InsufficientLeaveBalance(
                    f"Available {available}, requested {application.requested_days}, "
                    f"allowed negative {allowed}."
                )
            collector.add_if(
                available - application.requested_days < 0, WarningCode.LEAVE_NEGATIVE_BALANCE
            )
        effects = self._effects(context)
        if effects is not None:  # LEV-31 and LEV-30 are re-checked at final approval
            active = self._slots.for_application(application.id, status="ACTIVE")
            effects.assert_no_override_conflict(employee.id, active)
            collector.add_if(
                effects.punch_conflict(employee.id, active), WarningCode.LEAVE_PUNCH_CONFLICT
            )
        return collector.enforce()

    def _recalculate_pending(
        self,
        context: OrgContext,
        application: LeaveApplication,
        balance: LeaveBalance | None,
        version: LeavePolicyVersion,
    ) -> None:
        """Re-evaluate ACTIVE slots at final approval; adjusts the reservation by the delta."""
        stored = {
            (s.slot_date, s.half): s
            for s in self._slots.for_application(application.id, status="ACTIVE")
        }
        results, new_requested = evaluate_slots(
            application.start_date,
            Half(application.start_half),
            application.end_date,
            Half(application.end_half),
            self._day_type_fn(application.employee_id),
            count_weekly_offs=version.count_weekly_offs,
            count_holidays=version.count_holidays,
        )
        changed = False
        for result in results:
            slot = stored.get((result.slot_date, result.half.value))
            if slot is not None and (slot.day_type, slot.counted) != (
                result.day_type,
                result.counted,
            ):
                slot.day_type, slot.counted = result.day_type, result.counted
                changed = True
        if not changed:
            return
        if new_requested <= 0:
            raise LeaveZeroDuration()
        delta = new_requested - application.requested_days
        if balance is not None and application.reserves_balance:
            balance.reserved_days = balance.reserved_days + delta
        application.requested_days = new_requested
        self._record_action(
            context,
            application,
            ApplicationAction.DURATION_RECALCULATED,
            days_affected=abs(delta),
            comment="Duration recalculated against the current schedule.",
        )

    def reject(
        self,
        context: OrgContext,
        application_id: uuid.UUID,
        *,
        decision_comment: str,
        if_match: int | None,
    ) -> ApplicationView:
        self._require_module(context)
        _employee, balance, application, _type = self._lock_for_mutation(application_id)
        self._assert_decidable(application, if_match)
        comment = decision_comment.strip()
        if not DECISION_COMMENT_MIN <= len(comment) <= 1000:
            raise ValidationFailed("decision_comment must be 10 to 1,000 characters.").with_detail(
                field="decision_comment", code="INVALID_LENGTH", message="Explain the rejection."
            )
        authorize_decision(
            context,
            application,
            manager_permission=Permission.LEAVE_APPROVE,
            hr_permission=Permission.LEAVE_APPROVE_HR,
            comment=comment,
        )
        self._release(application, balance, "RELEASED")
        self._record_action(
            context,
            application,
            ApplicationAction.REJECTED,
            level=application.current_approval_level,
            from_status=ApplicationStatus.SUBMITTED.value,
            to_status=ApplicationStatus.REJECTED.value,
            comment=comment,
        )
        application.status = ApplicationStatus.REJECTED.value
        application.decided_by = context.user_id
        application.decided_at = utc_now()
        application.decision_comment = comment
        application.updated_by = context.user_id
        self._clear_routing(application)
        self._audit_application(
            context, application, AuditAction.LEAVE_APPLICATION_REJECTED, reason=comment
        )
        return self._view(application)

    def withdraw(
        self,
        context: OrgContext,
        application_id: uuid.UUID,
        *,
        reason: str | None,
        if_match: int | None,
    ) -> ApplicationView:
        self._require_module(context)
        _employee, balance, application, _type = self._lock_for_mutation(application_id)
        owner = context.is_self(application.employee_id) and context.has(Permission.SELF_LEAVE)
        if not (owner or context.has(Permission.LEAVE_APPLY_ON_BEHALF)):
            raise ResourceNotFound("leave application")
        assert_etag(application.row_version, if_match)
        if application.status != ApplicationStatus.SUBMITTED.value:
            raise InvalidApprovalTransition()
        self._release(application, balance, "RELEASED")
        self._record_action(
            context,
            application,
            ApplicationAction.WITHDRAWN,
            from_status=ApplicationStatus.SUBMITTED.value,
            to_status=ApplicationStatus.WITHDRAWN.value,
            comment=reason,
        )
        application.status = ApplicationStatus.WITHDRAWN.value
        application.decided_by = context.user_id
        application.decided_at = utc_now()
        application.updated_by = context.user_id
        self._clear_routing(application)
        self._audit_application(
            context, application, AuditAction.LEAVE_APPLICATION_WITHDRAWN, reason=reason
        )
        return self._view(application)

    # ------------------------------------------------------------------ cancellation (§6.12)

    def cancel(  # noqa: PLR0912, PLR0915 - the §6.12 table as one sequence
        self,
        context: OrgContext,
        application_id: uuid.UUID,
        *,
        scope: str,
        cancel_from_date: date | None,
        cancel_from_half: str | None,
        reason: str,
        restoration_mode: str | None,
        if_match: int | None,
    ) -> tuple[ApplicationView, Decimal]:
        self._require_module(context)
        employee, balance, application, leave_type = self._lock_for_mutation(application_id)
        owner = context.is_self(application.employee_id) and context.has(Permission.SELF_LEAVE)
        admin = context.has(Permission.LEAVE_CANCEL_ANY)
        if not (owner or admin):
            raise ResourceNotFound("leave application")
        assert_etag(application.row_version, if_match)
        if application.status != ApplicationStatus.APPROVED.value:
            raise LeaveNotCancellable()
        reason = reason.strip()
        if len(reason) < CANCEL_REASON_MIN:
            raise ValidationFailed(
                "A cancellation reason of at least 10 characters is required."
            ).with_detail(field="reason", code="TOO_SHORT", message="Explain the cancellation.")
        today = self._today(context)
        version = self._config.version_by_id(application.policy_version_id)
        if not admin and not version.employee_cancellation_allowed:
            raise LeaveNotCancellable("Employee cancellation is not allowed by the policy.")

        active = self._slots.for_application(application.id, status="ACTIVE")
        if scope == "FROM":
            if cancel_from_date is None or cancel_from_half is None:
                raise ValidationFailed(
                    "cancel_from_date and cancel_from_half are required for scope FROM."
                )
            origin = (cancel_from_date, cancel_from_half)
            target = [s for s in active if (s.slot_date, s.half) >= origin]
        else:
            target = list(active)
        if not target:
            raise LeaveNotCancellable()  # AC-46: a repeated request finds nothing left
        if not admin and any(s.slot_date <= today for s in target):
            raise LeaveNotCancellable("Employees can cancel only slots after today.")

        restored = HALF_DAY * sum(1 for s in target if s.counted)
        if restored > application.consumed_days:
            raise LeaveNotCancellable("Restoration would exceed the consumed days.")
        year_closed = balance is not None and balance.closure_status == "CLOSED"
        if year_closed:
            context.require(Permission.LEAVE_ADMIN_CORRECT)
            if restoration_mode not in ("RESTORE_TO_CURRENT_YEAR", "NO_RESTORATION"):
                raise RestorationModeRequired()
            if restoration_mode == "RESTORE_TO_CURRENT_YEAR" and len(reason) < 20:
                raise ValidationFailed(
                    "Restoring into the current year needs a reason of 20+ characters."
                )
        mode = restoration_mode if year_closed else "NORMAL"
        restored_posted = (
            Decimal("0.00") if year_closed and restoration_mode == "NO_RESTORATION" else restored
        )

        remaining = len(active) - len(target)
        full = remaining == 0
        action = self._record_action(
            context,
            application,
            ApplicationAction.CANCELLED if full else ApplicationAction.PARTIALLY_CANCELLED,
            from_status=ApplicationStatus.APPROVED.value,
            to_status=(ApplicationStatus.CANCELLED if full else ApplicationStatus.APPROVED).value,
            comment=reason,
            days_affected=restored,
            cancel_from_date=cancel_from_date if scope == "FROM" else None,
            cancel_from_half=cancel_from_half if scope == "FROM" else None,
            restoration_mode=mode,
        )
        for slot in target:
            slot.slot_status = "CANCELLED"
            slot.cancelled_by_action_id = action.id
        if balance is not None and restored_posted > 0:
            self._post_restoration(
                context,
                employee,
                leave_type,
                application,
                balance,
                action,
                restored_posted,
                mode,
                reason,
            )
        application.consumed_days = application.consumed_days - restored_posted
        application.cancelled_days = application.cancelled_days + restored_posted
        application.has_partial_cancellation = not full or application.has_partial_cancellation
        application.cancellation_reason = reason
        if full:
            application.status = ApplicationStatus.CANCELLED.value
        application.updated_by = context.user_id
        self._audit_application(
            context,
            application,
            AuditAction.LEAVE_APPLICATION_CANCELLED
            if full
            else AuditAction.LEAVE_APPLICATION_PARTIALLY_CANCELLED,
            after={"status": application.status, "restored_days": str(restored_posted)},
            reason=reason,
        )
        effects = self._effects(context)
        if effects is not None:  # INT-12: cancelled counted halves are re-evaluated from punches
            effects.on_cancelled(employee.id, application, target, context.user_id)
        return self._view(application), restored_posted

    def _post_restoration(  # noqa: PLR0917
        self,
        context: OrgContext,
        employee: Employee,
        leave_type: LeaveType,
        application: LeaveApplication,
        balance: LeaveBalance,
        action: LeaveApplicationAction,
        restored: Decimal,
        mode: str | None,
        reason: str,
    ) -> None:
        today = self._today(context)
        if mode == "RESTORE_TO_CURRENT_YEAR":
            year_start = leave_year_start(today, self._settings_row(context).leave_year_start_month)
            target_balance = self._ledger.lock_balance(employee.id, leave_type.id, year_start)
            effective = today
        else:
            year_start = application.leave_year_start
            target_balance = balance
            year_end = next_leave_year_start(year_start)
            effective = today if year_start <= today < year_end else year_end - timedelta(days=1)
        self._ledger.post(
            target_balance,
            Posting(
                employee_id=employee.id,
                leave_type_id=leave_type.id,
                year_start=year_start,
                transaction_type=TransactionType.CONSUMPTION_REVERSAL.value,
                quantity_days=restored,
                effective_date=effective,
                idempotency_key=f"CRV:{application.id}:{action.id}",
                actor_id=context.user_id,
                leave_application_id=application.id,
                application_action_id=action.id,
                reason=reason if mode == "RESTORE_TO_CURRENT_YEAR" else None,
            ),
        )

    # ---------------------------------------------------------- reassignment / recalculation

    def reassign(
        self,
        context: OrgContext,
        application_id: uuid.UUID,
        *,
        to_user_id: uuid.UUID | None,
        to_hr_queue: bool,
        reason: str,
        if_match: int | None,
    ) -> ApplicationView:
        """INT-35: HR moves the current level to a user or back to the HR queue."""
        self._require_module(context)
        context.require(Permission.LEAVE_APPROVE_HR)
        _employee, _balance, application, _type = self._lock_for_mutation(application_id)
        assert_etag(application.row_version, if_match)
        if application.status != ApplicationStatus.SUBMITTED.value:
            raise InvalidApprovalTransition()
        if (to_user_id is None) == (not to_hr_queue):
            raise ValidationFailed("Provide exactly one of to_user_id or to_hr_queue.")
        if len(reason.strip()) < DECISION_COMMENT_MIN:
            raise ValidationFailed("A reason of at least 10 characters is required.")
        route = Route(HR_QUEUE, None, None)
        if to_user_id is not None:
            user = self._users.get(to_user_id)
            if user is None or not user.is_active:
                raise ResourceNotFound("user")
            if (
                to_user_id == application.submitted_by
                or user.employee_id == application.employee_id
            ):
                raise SelfApprovalNotAllowed()
            if not self._access.resolve(to_user_id, context.organization_id).permissions.has_any(
                *_LEAVE_APPROVER_PERMISSIONS
            ):
                raise NotCurrentApprover("The target user cannot approve leave.")
            route = Route(MANAGER, to_user_id, user.employee_id)
        previous_type = application.current_approver_type
        previous_user = application.current_approver_user_id
        self._apply_route(application, application.current_approval_level or 1, route)
        self._record_action(
            context,
            application,
            ApplicationAction.REASSIGNED,
            level=application.current_approval_level,
            from_approver_type=previous_type,
            from_approver_user_id=previous_user,
            to_approver_type=route.approver_type,
            to_approver_user_id=route.user_id,
            comment=reason,
        )
        application.updated_by = context.user_id
        self._audit_application(
            context, application, AuditAction.LEAVE_APPLICATION_REASSIGNED, reason=reason
        )
        return self._view(application)

    def recalculate(
        self,
        context: OrgContext,
        application_id: uuid.UUID,
        *,
        reason: str,
        if_match: int | None,
    ) -> ApplicationView:
        """§6.13: recompute an APPROVED application's counted days against today's schedule."""
        self._require_module(context)
        context.require(Permission.LEAVE_ADMIN_CORRECT)
        employee, balance, application, leave_type = self._lock_for_mutation(application_id)
        assert_etag(application.row_version, if_match)
        if application.status != ApplicationStatus.APPROVED.value or application.holiday_id:
            raise InvalidApprovalTransition(
                "Only an APPROVED application that is not an optional holiday can be recalculated."
            )
        if len(reason.strip()) < 20:
            raise ValidationFailed("A reason of at least 20 characters is required.")
        version = self._config.version_by_id(application.policy_version_id)
        stored = self._slots.for_application(application.id, status="ACTIVE")
        results, _ = evaluate_slots(
            application.start_date,
            Half(application.start_half),
            application.end_date,
            Half(application.end_half),
            self._day_type_fn(application.employee_id),
            count_weekly_offs=version.count_weekly_offs,
            count_holidays=version.count_holidays,
        )
        fresh = {(r.slot_date, r.half.value): r for r in results}
        for slot in stored:
            result = fresh[(slot.slot_date, slot.half)]
            slot.day_type, slot.counted = result.day_type, result.counted
        new_consumed = HALF_DAY * sum(1 for s in stored if s.counted)
        delta = new_consumed - application.consumed_days
        if (
            delta > 0
            and balance is not None
            and (balance.available_days - delta < -_allowed_negative(version))
        ):
            raise InsufficientLeaveBalance()
        action = self._record_action(
            context,
            application,
            ApplicationAction.ADMIN_RECALCULATED,
            comment=reason,
            days_affected=abs(delta),
        )
        if delta != 0 and balance is not None:
            today = self._today(context)
            year_end = next_leave_year_start(application.leave_year_start)
            in_year = application.leave_year_start <= today < year_end
            self._ledger.post(
                balance,
                Posting(
                    employee_id=employee.id,
                    leave_type_id=leave_type.id,
                    year_start=application.leave_year_start,
                    transaction_type=(
                        TransactionType.CONSUMPTION
                        if delta > 0
                        else TransactionType.CONSUMPTION_REVERSAL
                    ).value,
                    quantity_days=-delta,
                    effective_date=today if in_year else year_end - timedelta(days=1),
                    idempotency_key=f"{'CON' if delta > 0 else 'CRV'}:{application.id}:{action.id}",
                    actor_id=context.user_id,
                    leave_application_id=application.id,
                    application_action_id=action.id,
                    reason=reason,
                ),
            )
        application.requested_days = new_consumed + application.cancelled_days
        application.consumed_days = new_consumed
        application.schedule_review_required = False
        application.updated_by = context.user_id
        self._audit_application(
            context, application, AuditAction.LEAVE_APPLICATION_RECALCULATED, reason=reason
        )
        return self._view(application)

    # ------------------------------------------------------------------ reads

    def _approver_user_ids(self, application: LeaveApplication) -> set[uuid.UUID]:
        """Current and past approvers: whoever appears as an actor other than the submitter."""
        past = {
            a.actor_user_id
            for a in self._actions.for_application(application.id)
            if a.action != ApplicationAction.SUBMITTED.value
        }
        if application.current_approver_user_id:
            past.add(application.current_approver_user_id)
        return past

    def _own(self, context: OrgContext, application: LeaveApplication) -> bool:
        return context.is_self(application.employee_id) and context.has(Permission.SELF_LEAVE)

    def get(self, context: OrgContext, application_id: uuid.UUID) -> tuple[ApplicationView, bool]:
        """Detail plus whether the caller may see the free-text reason (§10.5)."""
        application = self._applications.require(application_id, resource="leave application")
        own = self._own(context, application)
        approver = context.user_id in self._approver_user_ids(application)
        hr = context.has(Permission.LEAVE_APPROVE_HR)
        employee = self._employees.get(application.employee_id)
        in_scope = (
            context.has(Permission.LEAVE_VIEW)
            and employee is not None
            and employee_in_scope(context, self._employees, employee)
        )
        if not (own or approver or hr or in_scope):
            raise ResourceNotFound("leave application")
        return self._view(application), own or approver or hr

    def list_applications(
        self,
        context: OrgContext,
        *,
        employee_id: uuid.UUID | None,
        statuses: list[str] | None,
        leave_type_id: uuid.UUID | None,
        from_date: date | None,
        to_date: date | None,
        approver_me: bool,
        hr_queue: bool,
        limit: int,
        cursor: str | None,
    ) -> tuple[list[LeaveApplication], str | None]:
        employee_ids: list[uuid.UUID] | None = None
        approver_user_id: uuid.UUID | None = None
        queue = False
        if approver_me or hr_queue:
            if hr_queue:
                context.require(Permission.LEAVE_APPROVE_HR)
                queue = True
            if approver_me:
                context.require_any(Permission.LEAVE_APPROVE, Permission.LEAVE_APPROVE_HR)
                approver_user_id = context.user_id
        else:
            context.require(Permission.LEAVE_VIEW)
            if context.is_reporting_scoped:
                employee_ids = [
                    *self._employees.descendant_ids(context.employee_id or uuid.uuid4())
                ]
                if context.employee_id:
                    employee_ids.append(context.employee_id)
        statement = self._applications.list_query(
            employee_id=employee_id,
            employee_ids=employee_ids,
            statuses=statuses,
            leave_type_id=leave_type_id,
            from_date=from_date,
            to_date=to_date,
            approver_user_id=approver_user_id,
            hr_queue=queue,
        )
        return self._page(statement, limit, cursor)

    def list_own(
        self, context: OrgContext, *, statuses: list[str] | None, limit: int, cursor: str | None
    ) -> tuple[list[LeaveApplication], str | None]:
        employee_id = self.own_employee_id(context)
        statement = self._applications.list_query(employee_id=employee_id, statuses=statuses)
        return self._page(statement, limit, cursor)

    def _page(
        self, statement: Any, limit: int, cursor: str | None
    ) -> tuple[list[LeaveApplication], str | None]:
        """Keyset pagination on (submitted_at DESC, id DESC) with the signed cursor (§26.5)."""
        if cursor:
            payload = self._codec.decode(cursor)
            marker = datetime.fromisoformat(str(payload.sort_value))
            statement = statement.where(
                or_(
                    LeaveApplication.submitted_at < marker,
                    and_(
                        LeaveApplication.submitted_at == marker,
                        LeaveApplication.id < payload.last_id,
                    ),
                )
            )
        statement = statement.order_by(
            LeaveApplication.submitted_at.desc(), LeaveApplication.id.desc()
        ).limit(limit + 1)
        rows = list(self._applications.session.execute(statement).scalars())
        more = len(rows) > limit
        rows = rows[:limit]
        next_cursor = (
            self._codec.encode(
                sort_field="submitted_at",
                sort_value=rows[-1].submitted_at,
                last_id=rows[-1].id,
                direction=SortDirection.DESC,
            )
            if more
            else None
        )
        return rows, next_cursor

    # Lifecycle side effects (7.5, INT-36). Called inside the EMS transaction that triggers them,
    # with the employee row already locked by the caller.

    def reroute_for_manager_change(self, context: OrgContext, employee: Employee) -> int:
        """INT-36: SUBMITTED applications whose level is at MANAGER follow the new manager."""
        if not self._settings_row(context).enable_leave_module:
            return 0
        statement = self._applications.list_query(
            employee_id=employee.id, statuses=(ApplicationStatus.SUBMITTED.value,)
        ).order_by(LeaveApplication.id)
        changed = 0
        for listed in list(self._applications.session.execute(statement).scalars()):
            if listed.current_approver_type != MANAGER:
                continue
            application = self._applications.require(listed.id, resource="leave application")
            self._applications.session.refresh(application, with_for_update=True)
            route = self._router.route(
                employee, MANAGER, manager_permissions=_LEAVE_APPROVER_PERMISSIONS
            )
            previous = (application.current_approver_type, application.current_approver_user_id)
            self._apply_route(application, application.current_approval_level or 1, route)
            self._record_action(
                context,
                application,
                ApplicationAction.REASSIGNED,
                level=application.current_approval_level,
                from_approver_type=previous[0],
                from_approver_user_id=previous[1],
                to_approver_type=route.approver_type,
                to_approver_user_id=route.user_id,
                comment="MANAGER_CHANGE",
            )
            application.updated_by = context.user_id
            self._audit_application(
                context,
                application,
                AuditAction.LEAVE_APPLICATION_REASSIGNED,
                reason="MANAGER_CHANGE",
            )
            changed += 1
        return changed

    def system_cancel_for_employee(
        self,
        context: OrgContext,
        employee: Employee,
        *,
        reason_code: str,
        end_date: date | None,
    ) -> int:
        """7.5: pending leave past the employment end (or all pending on archive) is cancelled.

        SUBMITTED: SYSTEM_CANCELLED, reservation released, slots RELEASED. APPROVED with slots
        after ``end_date``: a tail cancellation from ``end_date + 1`` (NORMAL restoration, or
        NO_RESTORATION when the year is CLOSED). Approved leave is left alone on archive.
        """
        if not self._settings_row(context).enable_leave_module:
            return 0
        statement = self._applications.list_query(
            employee_id=employee.id,
            statuses=(ApplicationStatus.SUBMITTED.value, ApplicationStatus.APPROVED.value),
        ).order_by(LeaveApplication.id)
        changed = 0
        for listed in list(self._applications.session.execute(statement).scalars()):
            _employee, balance, application, leave_type = self._lock_for_mutation(listed.id)
            active = list(self._slots.for_application(application.id, status="ACTIVE"))
            if application.status == ApplicationStatus.SUBMITTED.value:
                if end_date is not None and not any(s.slot_date > end_date for s in active):
                    continue
                self._release(application, balance, "RELEASED")
                self._record_action(
                    context,
                    application,
                    ApplicationAction.SYSTEM_CANCELLED,
                    from_status=ApplicationStatus.SUBMITTED.value,
                    to_status=ApplicationStatus.CANCELLED.value,
                    comment=reason_code,
                )
                application.status = ApplicationStatus.CANCELLED.value
                application.cancelled_days = application.requested_days  # nothing was consumed
                application.cancellation_reason = reason_code
                application.updated_by = context.user_id
                self._clear_routing(application)
                self._audit_application(
                    context,
                    application,
                    AuditAction.LEAVE_APPLICATION_SYSTEM_CANCELLED,
                    reason=reason_code,
                )
                changed += 1
            elif end_date is not None:
                target = [s for s in active if s.slot_date > end_date]
                if not target:
                    continue
                self._system_tail_cancel(
                    context,
                    employee,
                    leave_type,
                    application,
                    balance,
                    active,
                    target,
                    end_date,
                    reason_code,
                )
                changed += 1
        return changed

    def _system_tail_cancel(  # noqa: PLR0917
        self,
        context: OrgContext,
        employee: Employee,
        leave_type: LeaveType,
        application: LeaveApplication,
        balance: LeaveBalance | None,
        active: list[LeaveApplicationSlot],
        target: list[LeaveApplicationSlot],
        end_date: date,
        reason_code: str,
    ) -> None:
        restored = HALF_DAY * sum(1 for s in target if s.counted)
        closed = balance is not None and balance.closure_status == "CLOSED"
        mode = "NO_RESTORATION" if closed else "NORMAL"
        full = len(active) == len(target)
        action = self._record_action(
            context,
            application,
            ApplicationAction.CANCELLED if full else ApplicationAction.PARTIALLY_CANCELLED,
            from_status=ApplicationStatus.APPROVED.value,
            to_status=(ApplicationStatus.CANCELLED if full else ApplicationStatus.APPROVED).value,
            comment=reason_code,
            days_affected=restored,
            cancel_from_date=end_date + timedelta(days=1),
            cancel_from_half=Half.FIRST_HALF.value,
            restoration_mode=mode,
        )
        for slot in target:
            slot.slot_status = "CANCELLED"
            slot.cancelled_by_action_id = action.id
        posted = Decimal("0.00") if closed else restored
        if balance is not None and posted > 0:
            self._post_restoration(
                context,
                employee,
                leave_type,
                application,
                balance,
                action,
                posted,
                mode,
                reason_code,
            )
        application.consumed_days = application.consumed_days - posted
        application.cancelled_days = application.cancelled_days + posted
        application.has_partial_cancellation = not full or application.has_partial_cancellation
        application.cancellation_reason = reason_code
        if full:
            application.status = ApplicationStatus.CANCELLED.value
        application.updated_by = context.user_id
        self._audit_application(
            context,
            application,
            AuditAction.LEAVE_APPLICATION_CANCELLED
            if full
            else AuditAction.LEAVE_APPLICATION_PARTIALLY_CANCELLED,
            after={"status": application.status, "restored_days": str(posted)},
            reason=reason_code,
        )
        effects = self._effects(context)
        if effects is not None:
            effects.on_cancelled(employee.id, application, target, context.user_id)


def _counts(day_type: str, version: LeavePolicyVersion) -> bool:
    if day_type == "WORKING_DAY":
        return True
    return version.count_weekly_offs if day_type == "WEEKLY_OFF" else version.count_holidays


def _all_non_working(first: date, last: date, day_type_of: Any) -> bool:
    day = first
    while day <= last:
        if day_type_of(day) == "WORKING_DAY":
            return False
        day += timedelta(days=1)
    return True


def _allowed_negative(version: LeavePolicyVersion) -> Decimal:
    if not version.allow_negative_balance:
        return Decimal(0)
    return version.max_negative_balance_days or Decimal(0)
