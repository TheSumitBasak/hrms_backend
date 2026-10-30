"""Work-schedule configuration and resolution (addendum §5.2, §5.6).

Owns weekly-off patterns, holiday calendars, holidays and branch calendar assignments, and the
leave-relevant part of ``SCHEDULE(E, D)``: the day type of an employee on a date. Steps 1-3 of
§5.6 (attendance policy and shift) belong to the attendance stage; when only leave is enabled
they yield nothing, which is exactly what this resolver assumes.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from app.audit.actions import AuditAction
from app.audit.writer import AuditWriter
from app.core.clock import today_in
from app.core.context import OrgContext
from app.core.errors import (
    DuplicateResource,
    InvalidOrganizationReference,
    MasterInactive,
    ModuleNotEnabled,
    ValidationFailed,
    WeeklyOffPatternInUse,
)
from app.core.permissions import Permission
from app.db.types import uuid7
from app.models.attendance import (
    BranchHolidayCalendarAssignment,
    Holiday,
    HolidayCalendar,
    HolidayStatus,
    WeeklyOffPattern,
    WeeklyOffPatternDay,
)
from app.models.employee import Employee
from app.repositories.attendance import (
    AttendancePolicyVersionRepository,
    ShiftAssignmentRepository,
    ShiftRepository,
)
from app.repositories.branch import BranchRepository
from app.repositories.employee import EmployeeRepository
from app.repositories.organization import (
    OrganizationRepository,
    OrganizationSettingsRepository,
)
from app.repositories.schedule import (
    BranchCalendarAssignmentRepository,
    EmployeeBranchResolver,
    HolidayCalendarRepository,
    HolidayRepository,
    WeeklyOffPatternRepository,
)
from app.services.organizations import assert_etag
from app.services.schedule_resolver import ScheduleResolver
from app.services.schedule_rules import validate_pattern_rules

MAX_RESOLVED_HOLIDAY_DAYS = 366
"""§5.16: the per-employee holiday lookup spans at most 366 days."""

_ATTENDANCE_OR_LEAVE_VIEW = (
    Permission.ATTENDANCE_VIEW,
    Permission.LEAVE_TYPE_MANAGE,
    Permission.ATTENDANCE_POLICY_MANAGE,
)
_PATTERN_MANAGE = (Permission.ATTENDANCE_POLICY_MANAGE, Permission.LEAVE_TYPE_MANAGE)


@dataclass(frozen=True, slots=True)
class DayType:
    """Result of the §5.6 day-type step for one employee and date."""

    day_type: str  # WORKING_DAY | WEEKLY_OFF | HOLIDAY
    holiday: Holiday | None
    calendar_id: uuid.UUID | None
    pattern_id: uuid.UUID | None


class ScheduleService:
    def __init__(
        self,
        *,
        patterns: WeeklyOffPatternRepository,
        calendars: HolidayCalendarRepository,
        holidays: HolidayRepository,
        assignments: BranchCalendarAssignmentRepository,
        branches: BranchRepository,
        employees: EmployeeRepository,
        branch_resolver: EmployeeBranchResolver,
        organizations: OrganizationRepository,
        settings: OrganizationSettingsRepository,
        audit: AuditWriter,
        shift_assignments: ShiftAssignmentRepository,
        policy_versions: AttendancePolicyVersionRepository,
        shifts: ShiftRepository,
    ) -> None:
        self._shift_assignments = shift_assignments
        self._policy_versions = policy_versions
        self._shifts = shifts
        self._patterns = patterns
        self._calendars = calendars
        self._holidays = holidays
        self._assignments = assignments
        self._branches = branches
        self._employees = employees
        self._branch_resolver = branch_resolver
        self._organizations = organizations
        self._settings = settings
        self._audit = audit

    # ---------------------------------------------------------------- guards

    def _today(self, context: OrgContext) -> date:
        organization = self._organizations.get(context.organization_id)
        return today_in(organization.timezone if organization else None)

    def _require_module(self, context: OrgContext) -> None:
        """§5.2: schedules exist whenever attendance *or* leave is enabled (§9.1)."""
        settings = self._settings.require(context.organization_id)
        if not (settings.enable_attendance_module or settings.enable_leave_module):
            raise ModuleNotEnabled()

    # ------------------------------------------------------- weekly-off patterns

    def list_patterns(
        self, context: OrgContext, *, status: str | None
    ) -> tuple[list[WeeklyOffPattern], dict[uuid.UUID, list[WeeklyOffPatternDay]]]:
        context.require_any(*_ATTENDANCE_OR_LEAVE_VIEW)
        rows = list(self._patterns.list_patterns(status=status))
        return rows, self._patterns.days_for([row.id for row in rows])

    def get_pattern(
        self, context: OrgContext, pattern_id: uuid.UUID
    ) -> tuple[WeeklyOffPattern, list[WeeklyOffPatternDay]]:
        context.require_any(*_ATTENDANCE_OR_LEAVE_VIEW)
        pattern = self._patterns.require(pattern_id, resource="weekly-off pattern")
        return pattern, self._patterns.days_for([pattern.id])[pattern.id]

    def create_pattern(
        self, context: OrgContext, *, values: dict[str, Any]
    ) -> tuple[WeeklyOffPattern, list[WeeklyOffPatternDay]]:
        context.require_any(*_PATTERN_MANAGE)
        self._require_module(context)
        code = str(values["pattern_code"])
        if self._patterns.code_exists(code):
            raise DuplicateResource("A weekly-off pattern with that code already exists.")
        day_rules = [(d["day_of_week"], d["occurrence"]) for d in values["days"]]
        self._assert_rules_valid(day_rules)

        pattern = WeeklyOffPattern(
            id=uuid7(),
            organization_id=context.organization_id,
            pattern_code=code,
            pattern_name=str(values["pattern_name"]),
            status="ACTIVE",
            created_by=context.user_id,
        )
        self._patterns.add(pattern)
        self._patterns.flush()
        days = self._day_rows(context, pattern.id, day_rules)
        self._patterns.replace_days(pattern.id, days)
        self._audit.record_change(
            context,
            action=AuditAction.WEEKLY_OFF_PATTERN_CREATED,
            entity_type="weekly_off_pattern",
            entity_id=pattern.id,
            after={"pattern_code": code, "pattern_name": pattern.pattern_name, "days": day_rules},
            fields=["pattern_code", "pattern_name", "days"],
        )
        return pattern, days

    def update_pattern(
        self,
        context: OrgContext,
        *,
        pattern_id: uuid.UUID,
        changes: dict[str, Any],
        if_match: int | None,
    ) -> tuple[WeeklyOffPattern, list[WeeklyOffPatternDay]]:
        context.require_any(*_PATTERN_MANAGE)
        self._require_module(context)
        pattern = self._patterns.require(pattern_id, resource="weekly-off pattern")
        assert_etag(pattern.row_version, if_match)

        before: dict[str, Any] = {}
        after: dict[str, Any] = {}
        for field in ("pattern_name", "status"):
            if field in changes and changes[field] is not None:
                before[field] = getattr(pattern, field)
                setattr(pattern, field, changes[field])
                after[field] = changes[field]

        days = self._patterns.days_for([pattern.id])[pattern.id]
        if changes.get("days") is not None:
            if self._pattern_in_use(pattern.id):
                raise WeeklyOffPatternInUse()
            day_rules = [(d["day_of_week"], d["occurrence"]) for d in changes["days"]]
            self._assert_rules_valid(day_rules)
            before["days"] = [(d.day_of_week, d.occurrence) for d in days]
            after["days"] = day_rules
            days = self._day_rows(context, pattern.id, day_rules)
            self._patterns.replace_days(pattern.id, days)

        if after:
            pattern.updated_by = context.user_id
            self._audit.record_change(
                context,
                action=AuditAction.WEEKLY_OFF_PATTERN_UPDATED,
                entity_type="weekly_off_pattern",
                entity_id=pattern.id,
                before=before,
                after=after,
                fields=list(after),
            )
        return pattern, days

    def _pattern_in_use(self, pattern_id: uuid.UUID) -> bool:
        """§5.2: referenced by the organization default, a shift assignment or a policy.

        Shift assignments and policy versions arrive with the attendance stage; extend this
        check when those tables exist.
        """
        settings = self._settings.require(self._patterns.organization_id)
        return settings.default_weekly_off_pattern_id == pattern_id

    @staticmethod
    def _assert_rules_valid(rules: list[tuple[int, str]]) -> None:
        problem = validate_pattern_rules(rules)
        if problem is not None:
            raise ValidationFailed(problem)

    @staticmethod
    def _day_rows(
        context: OrgContext, pattern_id: uuid.UUID, rules: list[tuple[int, str]]
    ) -> list[WeeklyOffPatternDay]:
        return [
            WeeklyOffPatternDay(
                id=uuid7(),
                organization_id=context.organization_id,
                pattern_id=pattern_id,
                day_of_week=dow,
                occurrence=occurrence,
                created_by=context.user_id,
            )
            for dow, occurrence in rules
        ]

    # ------------------------------------------------------------- calendars

    def list_calendars(self, context: OrgContext, *, status: str | None) -> list[HolidayCalendar]:
        return list(self._calendars.list_calendars(status=status))

    def get_calendar(self, context: OrgContext, calendar_id: uuid.UUID) -> HolidayCalendar:
        return self._calendars.require(calendar_id, resource="holiday calendar")

    def create_calendar(self, context: OrgContext, *, values: dict[str, Any]) -> HolidayCalendar:
        context.require(Permission.HOLIDAY_MANAGE)
        self._require_module(context)
        code = str(values["calendar_code"])
        if self._calendars.code_exists(code):
            raise DuplicateResource("A holiday calendar with that code already exists.")
        if values["is_organization_default"] and self._calendars.active_default() is not None:
            raise DuplicateResource("The organization already has an active default calendar.")
        calendar = HolidayCalendar(
            id=uuid7(),
            organization_id=context.organization_id,
            calendar_code=code,
            calendar_name=str(values["calendar_name"]),
            is_organization_default=bool(values["is_organization_default"]),
            status="ACTIVE",
            created_by=context.user_id,
        )
        self._calendars.add(calendar)
        self._calendars.flush()
        self._audit.record_change(
            context,
            action=AuditAction.HOLIDAY_CALENDAR_CREATED,
            entity_type="holiday_calendar",
            entity_id=calendar.id,
            after={
                "calendar_code": code,
                "calendar_name": calendar.calendar_name,
                "is_organization_default": calendar.is_organization_default,
            },
            fields=["calendar_code", "calendar_name", "is_organization_default"],
        )
        return calendar

    def update_calendar(
        self,
        context: OrgContext,
        *,
        calendar_id: uuid.UUID,
        changes: dict[str, Any],
        if_match: int | None,
    ) -> HolidayCalendar:
        context.require(Permission.HOLIDAY_MANAGE)
        self._require_module(context)
        calendar = self._calendars.require(calendar_id, resource="holiday calendar")
        assert_etag(calendar.row_version, if_match)
        applied = {k: v for k, v in changes.items() if v is not None}
        becomes_default = applied.get("is_organization_default", calendar.is_organization_default)
        becomes_active = applied.get("status", calendar.status) == "ACTIVE"
        if becomes_default and becomes_active:
            other = self._calendars.active_default()
            if other is not None and other.id != calendar.id:
                raise DuplicateResource("The organization already has an active default calendar.")
        before = {field: getattr(calendar, field) for field in applied}
        for field, value in applied.items():
            setattr(calendar, field, value)
        if applied:
            calendar.updated_by = context.user_id
            self._audit.record_change(
                context,
                action=AuditAction.HOLIDAY_CALENDAR_UPDATED,
                entity_type="holiday_calendar",
                entity_id=calendar.id,
                before=before,
                after=applied,
                fields=list(applied),
            )
        return calendar

    # -------------------------------------------------------------- holidays

    def list_holidays(
        self,
        context: OrgContext,
        *,
        calendar_id: uuid.UUID,
        year: int | None,
        date_range: tuple[date, date] | None,
        include_cancelled: bool,
    ) -> list[Holiday]:
        """Default is the current calendar year in the organization's timezone."""
        self._calendars.require(calendar_id, resource="holiday calendar")
        if date_range is not None:
            start, end = date_range
        else:
            chosen = year if year is not None else self._today(context).year
            start, end = date(chosen, 1, 1), date(chosen, 12, 31)
        if end < start or (end - start).days > MAX_RESOLVED_HOLIDAY_DAYS:
            raise ValidationFailed("The date range must be 1 to 366 days.")
        return list(
            self._holidays.list_for_calendar(
                calendar_id, start=start, end=end, include_cancelled=include_cancelled
            )
        )

    def add_holiday(
        self, context: OrgContext, *, calendar_id: uuid.UUID, values: dict[str, Any]
    ) -> Holiday:
        context.require(Permission.HOLIDAY_MANAGE)
        self._require_module(context)
        calendar = self._calendars.require(calendar_id, resource="holiday calendar")
        if calendar.status != "ACTIVE":
            raise MasterInactive("The holiday calendar is inactive.")
        holiday_date: date = values["holiday_date"]
        self._require_correct_for_past(context, holiday_date)
        if self._holidays.active_on(calendar.id, holiday_date) is not None:
            raise DuplicateResource("The calendar already has an active holiday on that date.")
        holiday = Holiday(
            id=uuid7(),
            organization_id=context.organization_id,
            calendar_id=calendar.id,
            holiday_date=holiday_date,
            holiday_name=str(values["holiday_name"]),
            holiday_type=str(values["holiday_type"]),
            status=HolidayStatus.ACTIVE.value,
            created_by=context.user_id,
        )
        self._holidays.add(holiday)
        self._holidays.flush()
        self._audit.record_change(
            context,
            action=AuditAction.HOLIDAY_ADDED,
            entity_type="holiday",
            entity_id=holiday.id,
            after={
                "calendar_id": calendar.id,
                "holiday_date": holiday_date.isoformat(),
                "holiday_name": holiday.holiday_name,
                "holiday_type": holiday.holiday_type,
            },
            fields=["calendar_id", "holiday_date", "holiday_name", "holiday_type"],
        )
        return holiday

    def rename_holiday(
        self, context: OrgContext, *, holiday_id: uuid.UUID, name: str, if_match: int | None
    ) -> Holiday:
        context.require(Permission.HOLIDAY_MANAGE)
        self._require_module(context)
        holiday = self._holidays.require(holiday_id, resource="holiday")
        assert_etag(holiday.row_version, if_match)
        if holiday.status != HolidayStatus.ACTIVE.value:
            raise ValidationFailed("A cancelled holiday cannot be renamed.")
        before = {"holiday_name": holiday.holiday_name}
        holiday.holiday_name = name
        holiday.updated_by = context.user_id
        self._audit.record_change(
            context,
            action=AuditAction.HOLIDAY_RENAMED,
            entity_type="holiday",
            entity_id=holiday.id,
            before=before,
            after={"holiday_name": name},
            fields=["holiday_name"],
        )
        return holiday

    def cancel_holiday(
        self, context: OrgContext, *, holiday_id: uuid.UUID, reason: str, if_match: int | None
    ) -> Holiday:
        context.require(Permission.HOLIDAY_MANAGE)
        self._require_module(context)
        holiday = self._holidays.require(holiday_id, resource="holiday")
        assert_etag(holiday.row_version, if_match)
        if holiday.status != HolidayStatus.ACTIVE.value:
            raise ValidationFailed("The holiday is already cancelled.")
        self._require_correct_for_past(context, holiday.holiday_date)
        holiday.status = HolidayStatus.CANCELLED.value
        holiday.cancellation_reason = reason
        holiday.updated_by = context.user_id
        self._audit.record_change(
            context,
            action=AuditAction.HOLIDAY_CANCELLED,
            entity_type="holiday",
            entity_id=holiday.id,
            before={"status": HolidayStatus.ACTIVE.value},
            after={"status": HolidayStatus.CANCELLED.value},
            fields=["status"],
            reason=reason,
        )
        return holiday

    def _require_correct_for_past(self, context: OrgContext, holiday_date: date) -> None:
        """§5.16: changing a past date also needs ``attendance.correct`` (INT-21)."""
        if holiday_date < self._today(context):
            context.require(Permission.ATTENDANCE_CORRECT)

    # --------------------------------------------------- branch calendar assignments

    def list_assignments(
        self, context: OrgContext, *, branch_id: uuid.UUID
    ) -> list[BranchHolidayCalendarAssignment]:
        context.require(Permission.HOLIDAY_MANAGE)
        self._branches.require(branch_id, resource="branch")
        return list(self._assignments.for_branch(branch_id))

    def assign_calendar(
        self, context: OrgContext, *, branch_id: uuid.UUID, values: dict[str, Any]
    ) -> BranchHolidayCalendarAssignment:
        context.require(Permission.HOLIDAY_MANAGE)
        self._require_module(context)
        branch = self._branches.lock(branch_id)  # serialises assignments for one branch
        calendar = self._calendars.get(values["calendar_id"])
        if calendar is None:
            raise InvalidOrganizationReference("calendar_id")
        if calendar.status != "ACTIVE":
            raise MasterInactive("The holiday calendar is inactive.")
        start: date = values["effective_from"]
        end: date | None = values.get("effective_to")
        for existing in self._assignments.for_branch(branch.id):
            if _overlaps(start, end, existing.effective_from, existing.effective_to):
                raise ValidationFailed(
                    "The branch already has a calendar assignment overlapping that period."
                )
        row = BranchHolidayCalendarAssignment(
            id=uuid7(),
            organization_id=context.organization_id,
            branch_id=branch.id,
            calendar_id=calendar.id,
            effective_from=start,
            effective_to=end,
            created_by=context.user_id,
        )
        self._assignments.add(row)
        self._assignments.flush()
        self._audit.record_change(
            context,
            action=AuditAction.HOLIDAY_CALENDAR_ASSIGNED,
            entity_type="branch_holiday_calendar_assignment",
            entity_id=row.id,
            after={
                "branch_id": branch.id,
                "calendar_id": calendar.id,
                "effective_from": start.isoformat(),
                "effective_to": end.isoformat() if end else None,
            },
            fields=["branch_id", "calendar_id", "effective_from", "effective_to"],
        )
        return row

    # ------------------------------------------------------------- resolution

    def calendar_id_for(self, employee_id: uuid.UUID, day: date) -> uuid.UUID | None:
        """§5.6 step 5: the branch's assigned calendar on ``day``, else the org default."""
        branch_id = self._branch_resolver.branch_as_of(employee_id, day)
        if branch_id is not None:
            assignment = self._assignments.covering(branch_id, day)
            if assignment is not None:
                return assignment.calendar_id
        default = self._calendars.active_default()
        return default.id if default is not None else None

    def resolver(self, employee: Employee) -> ScheduleResolver:
        """A cached SCHEDULE(E, ·) for one employee (5.6); build once per request and reuse."""
        settings = self._settings.require(self._patterns.organization_id)
        return ScheduleResolver(
            employee=employee,
            default_pattern_id=settings.default_weekly_off_pattern_id,
            attendance_enabled=bool(settings.enable_attendance_module),
            patterns=self._patterns,
            calendars=self._calendars,
            holidays=self._holidays,
            branch_assignments=self._assignments,
            shift_assignments=self._shift_assignments,
            policy_versions=self._policy_versions,
            shifts=self._shifts,
        )

    def day_type(self, employee_id: uuid.UUID, day: date) -> DayType:
        """5.6 for one date. Prefer :meth:`resolver` when resolving a range."""
        schedule = self.resolver(self._employees.require(employee_id, resource="employee")).at(day)
        return DayType(
            schedule.day_type, schedule.holiday, schedule.calendar_id, schedule.pattern_id
        )

    def holidays_for_employee(
        self, context: OrgContext, *, employee_id: uuid.UUID, start: date, end: date
    ) -> list[Holiday]:
        """Resolved holidays of an employee over ``[start, end]`` (§5.16, ≤ 366 days).

        shortcut: resolves day by day (≤ 366 small queries); batch by assignment interval if
        this shows up in profiling.
        """
        if not (
            context.is_self(employee_id)
            or context.has_any(Permission.ATTENDANCE_VIEW, Permission.LEAVE_VIEW)
        ):
            context.require(Permission.ATTENDANCE_VIEW)
        self._employees.require(employee_id, resource="employee")
        if end < start or (end - start).days >= MAX_RESOLVED_HOLIDAY_DAYS:
            raise ValidationFailed("The date range must be 1 to 366 days.")
        found: list[Holiday] = []
        day = start
        while day <= end:
            calendar_id = self.calendar_id_for(employee_id, day)
            if calendar_id is not None:
                holiday = self._holidays.active_on(calendar_id, day)
                if holiday is not None:
                    found.append(holiday)
            day += timedelta(days=1)
        return found


def _overlaps(start: date, end: date | None, other_start: date, other_end: date | None) -> bool:
    """Half-open interval overlap; ``None`` is open-ended."""
    return (end is None or other_start < end) and (other_end is None or start < other_end)
