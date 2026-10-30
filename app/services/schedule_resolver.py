"""SCHEDULE(E, D) of addendum 5.6, resolved from cached data (one resolver per employee).

Leave calls this once per slot of a range; loading assignments, policy versions, pattern rules,
branch history and holidays once (and by year) replaces roughly five queries per date.
With the attendance module disabled, steps 1-3 yield nothing and steps 4-6 behave exactly as the
leave-only resolver always did.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select

from app.models.attendance import (
    AttendancePolicyVersion,
    BranchHolidayCalendarAssignment,
    EmployeeShiftAssignment,
    Holiday,
    HolidayType,
    Shift,
)
from app.models.employee import Employee, EmployeeJobHistory
from app.repositories.attendance import (
    AttendancePolicyVersionRepository,
    ShiftAssignmentRepository,
    ShiftRepository,
)
from app.repositories.schedule import (
    BranchCalendarAssignmentRepository,
    HolidayCalendarRepository,
    HolidayRepository,
    WeeklyOffPatternRepository,
)
from app.services.schedule_rules import is_weekly_off
from app.services.versioning import select_version

POLICY_ATTRIBUTES = (
    "branch_id",
    "department_id",
    "work_location_id",
    "employment_type_id",
    "employee_category_id",
)


@dataclass(frozen=True, slots=True)
class Schedule:
    day_type: str  # WORKING_DAY | WEEKLY_OFF | HOLIDAY
    holiday: Holiday | None
    calendar_id: uuid.UUID | None
    pattern_id: uuid.UUID | None
    policy_version: AttendancePolicyVersion | None = None
    shift: Shift | None = None
    assignment: EmployeeShiftAssignment | None = None


class ScheduleResolver:
    def __init__(
        self,
        *,
        employee: Employee,
        default_pattern_id: uuid.UUID | None,
        attendance_enabled: bool,
        patterns: WeeklyOffPatternRepository,
        calendars: HolidayCalendarRepository,
        holidays: HolidayRepository,
        branch_assignments: BranchCalendarAssignmentRepository,
        shift_assignments: ShiftAssignmentRepository,
        policy_versions: AttendancePolicyVersionRepository,
        shifts: ShiftRepository,
    ) -> None:
        self._employee = employee
        self._default_pattern_id = default_pattern_id
        self._attendance = attendance_enabled
        self._patterns = patterns
        self._calendars = calendars
        self._holidays = holidays
        self._branch_assignments = branch_assignments
        self._shift_assignments = shift_assignments
        self._policy_versions = policy_versions
        self._shifts = shifts
        self._rules: dict[uuid.UUID, list[tuple[int, str]]] = {}
        self._history: list[tuple[date, int, uuid.UUID | None]] | None = None
        self._branch_cal: dict[uuid.UUID, list[BranchHolidayCalendarAssignment]] = {}
        self._holiday_years: dict[tuple[uuid.UUID, int], dict[date, Holiday]] = {}
        self._assignments: list[EmployeeShiftAssignment] | None = None
        self._versions: list[tuple[AttendancePolicyVersion, str]] | None = None
        self._shift_rows: dict[uuid.UUID, Shift] = {}
        self._default_calendar: tuple[uuid.UUID | None] | None = None

    # ------------------------------------------------------------------ cached loads

    def _pattern_rules(self, pattern_id: uuid.UUID) -> list[tuple[int, str]]:
        if pattern_id not in self._rules:
            rows = self._patterns.days_for([pattern_id])[pattern_id]
            self._rules[pattern_id] = [(r.day_of_week, r.occurrence) for r in rows]
        return self._rules[pattern_id]

    def _branch_on(self, day: date) -> uuid.UUID | None:
        if self._history is None:
            statement = (
                select(
                    EmployeeJobHistory.effective_date,
                    EmployeeJobHistory.sequence_no,
                    EmployeeJobHistory.branch_id,
                )
                .where(
                    EmployeeJobHistory.organization_id == self._patterns.organization_id,
                    EmployeeJobHistory.employee_id == self._employee.id,
                )
                .order_by(EmployeeJobHistory.effective_date, EmployeeJobHistory.sequence_no)
            )
            self._history = [
                (d, n, b) for d, n, b in self._patterns.session.execute(statement).all()
            ]
        found: uuid.UUID | None = None
        for effective, _seq, branch in self._history:
            if effective <= day:
                found = branch
        return found

    def _calendar_on(self, day: date) -> uuid.UUID | None:
        """5.6 step 5: the branch's assigned calendar, else the organization default."""
        branch = self._branch_on(day)
        if branch is not None:
            if branch not in self._branch_cal:
                self._branch_cal[branch] = list(self._branch_assignments.for_branch(branch))
            for row in self._branch_cal[branch]:
                if row.effective_from <= day and (
                    row.effective_to is None or row.effective_to > day
                ):
                    return row.calendar_id
        if self._default_calendar is None:
            default = self._calendars.active_default()
            self._default_calendar = (default.id if default else None,)
        return self._default_calendar[0]

    def _mandatory_holiday(self, calendar_id: uuid.UUID, day: date) -> Holiday | None:
        key = (calendar_id, day.year)
        if key not in self._holiday_years:
            rows = self._holidays.active_between(
                calendar_id, date(day.year, 1, 1), date(day.year, 12, 31)
            )
            self._holiday_years[key] = {
                h.holiday_date: h for h in rows if h.holiday_type == HolidayType.MANDATORY.value
            }
        return self._holiday_years[key].get(day)

    def _assignment_on(self, day: date) -> EmployeeShiftAssignment | None:
        if self._assignments is None:
            self._assignments = list(self._shift_assignments.for_employee(self._employee.id))
        for row in self._assignments:
            if row.effective_from <= day and (row.effective_to is None or day < row.effective_to):
                return row
        return None

    def _shift(self, shift_id: uuid.UUID) -> Shift:
        if shift_id not in self._shift_rows:
            self._shift_rows[shift_id] = self._shifts.require(shift_id, resource="shift")
        return self._shift_rows[shift_id]

    # ------------------------------------------------------------------ public

    def policy_version_on(self, day: date) -> AttendancePolicyVersion | None:
        """ATT-07 (using current employee attributes; see DEC-OAL-7)."""
        if not self._attendance:
            return None
        if self._versions is None:
            self._versions = self._policy_versions.active_with_codes()
        codes = {v.id: code for v, code in self._versions}
        return select_version(
            [v for v, _ in self._versions],
            self._employee,
            day,
            attributes=POLICY_ATTRIBUTES,
            policy_code=lambda v: codes[v.id],
        )

    def at(self, day: date) -> Schedule:
        version = self.policy_version_on(day)
        assignment = self._assignment_on(day) if self._attendance else None
        shift_id = (
            assignment.shift_id
            if assignment is not None and assignment.shift_id is not None
            else (version.default_shift_id if version is not None else None)
        )
        shift = self._shift(shift_id) if shift_id is not None else None
        pattern_id = (
            assignment.weekly_off_pattern_id
            if assignment is not None and assignment.weekly_off_pattern_id is not None
            else (version.default_weekly_off_pattern_id if version is not None else None)
        ) or self._default_pattern_id

        calendar_id = self._calendar_on(day)
        holiday = self._mandatory_holiday(calendar_id, day) if calendar_id is not None else None
        if holiday is not None:
            kind = "HOLIDAY"
        elif pattern_id is not None and is_weekly_off(day, self._pattern_rules(pattern_id)):
            kind = "WEEKLY_OFF"
        else:
            kind = "WORKING_DAY"
        return Schedule(kind, holiday, calendar_id, pattern_id, version, shift, assignment)

    def day_type(self, day: date) -> str:
        return self.at(day).day_type
