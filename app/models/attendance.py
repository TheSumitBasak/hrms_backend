"""Work schedules and attendance (addendum §5).

This module is built in stages. Stage 1 (below) is the work-schedule configuration shared with
Leave (§5.2): weekly-off patterns, holiday calendars, holidays and branch calendar assignments.
Shifts, policies, punches and records are added by later stages.

MySQL notes (addendum §8.7 gives PostgreSQL definitions):

* a partial unique index is emulated with a stored generated column that is NULL outside the
  predicate, which a UNIQUE index ignores;
* an exclusion constraint on a date range cannot exist, so non-overlap of effective-dated rows
  is enforced by the service under a row lock on the parent, plus the nightly integrity check.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, time
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Computed,
    Date,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    SmallInteger,
    String,
    Time,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.mixins import (
    AppendOnlyColumns,
    StandardColumns,
    archive_reason_constraint,
    status_constraint,
    tenant_index,
)
from app.db.types import UtcDateTime, Uuid7

CODE_PATTERN_SQL = "'^[A-Z0-9][A-Z0-9_./-]{0,29}$'"
"""§21.1 V-CODE, shared with the masters and the job-architecture catalogues."""


class WeeklyOffOccurrence(StrEnum):
    """§5.2 ``weekly_off_pattern_days.occurrence``."""

    ALL = "ALL"
    FIRST = "FIRST"
    SECOND = "SECOND"
    THIRD = "THIRD"
    FOURTH = "FOURTH"
    FIFTH = "FIFTH"
    LAST = "LAST"


class HolidayType(StrEnum):
    MANDATORY = "MANDATORY"
    OPTIONAL = "OPTIONAL"


class HolidayStatus(StrEnum):
    ACTIVE = "ACTIVE"
    CANCELLED = "CANCELLED"


def _in(column: str, enum: type[StrEnum]) -> str:
    return f"{column} IN ({','.join(repr(member.value) for member in enum)})"


def _organization_column() -> Mapped[uuid.UUID]:
    return mapped_column(
        Uuid7, ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False, index=True
    )


class WeeklyOffPattern(Base, StandardColumns):
    """A named set of weekly-off rules (§5.2). A pattern with no day rows means no weekly offs."""

    __tablename__ = "weekly_off_patterns"
    __table_args__ = (
        UniqueConstraint(
            "organization_id", "pattern_code_normalized", name="uq_weekly_off_patterns_org_code"
        ),
        UniqueConstraint("organization_id", "id", name="uq_weekly_off_patterns_org_id"),
        CheckConstraint(
            f"pattern_code REGEXP {CODE_PATTERN_SQL}", name="ck_weekly_off_patterns_code_pattern"
        ),
        CheckConstraint(
            "CHAR_LENGTH(pattern_name) BETWEEN 2 AND 100", name="ck_weekly_off_patterns_name_length"
        ),
        status_constraint("weekly_off_patterns"),
        archive_reason_constraint("weekly_off_patterns"),
        tenant_index("weekly_off_patterns", "status", name="ix_weekly_off_patterns_org_status"),
    )

    organization_id: Mapped[uuid.UUID] = _organization_column()
    pattern_code: Mapped[str] = mapped_column(String(30), nullable=False)
    pattern_code_normalized: Mapped[str] = mapped_column(
        String(30), Computed("UPPER(pattern_code)", persisted=True), nullable=False
    )
    pattern_name: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(
        String(10), nullable=False, default="ACTIVE", server_default="ACTIVE"
    )


class WeeklyOffPatternDay(Base, AppendOnlyColumns):
    """One rule row of a pattern. Rows are replaced, never edited, while unreferenced."""

    __tablename__ = "weekly_off_pattern_days"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_weekly_off_pattern_days_org_id"),
        ForeignKeyConstraint(
            ["organization_id", "pattern_id"],
            ["weekly_off_patterns.organization_id", "weekly_off_patterns.id"],
            name="fk_weekly_off_pattern_days_pattern",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "pattern_id", "day_of_week", "occurrence", name="uq_weekly_off_pattern_days_rule"
        ),
        CheckConstraint(
            "day_of_week BETWEEN 1 AND 7", name="ck_weekly_off_pattern_days_day_of_week"
        ),
        CheckConstraint(
            _in("occurrence", WeeklyOffOccurrence), name="ck_weekly_off_pattern_days_occurrence"
        ),
        tenant_index(
            "weekly_off_pattern_days", "pattern_id", name="ix_weekly_off_days_org_pattern"
        ),
    )

    organization_id: Mapped[uuid.UUID] = _organization_column()
    pattern_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    day_of_week: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    occurrence: Mapped[str] = mapped_column(String(10), nullable=False)


class HolidayCalendar(Base, StandardColumns):
    """A holiday calendar (§5.2). Calendars are not bound to a year."""

    __tablename__ = "holiday_calendars"
    __table_args__ = (
        UniqueConstraint(
            "organization_id", "calendar_code_normalized", name="uq_holiday_calendars_org_code"
        ),
        UniqueConstraint("organization_id", "id", name="uq_holiday_calendars_org_id"),
        # At most one ACTIVE default per organization: the key is NULL for every other row.
        UniqueConstraint(
            "organization_id", "active_default_key", name="uq_holiday_calendars_active_default"
        ),
        CheckConstraint(
            f"calendar_code REGEXP {CODE_PATTERN_SQL}", name="ck_holiday_calendars_code_pattern"
        ),
        CheckConstraint(
            "CHAR_LENGTH(calendar_name) BETWEEN 2 AND 150", name="ck_holiday_calendars_name_length"
        ),
        status_constraint("holiday_calendars"),
        archive_reason_constraint("holiday_calendars"),
        tenant_index("holiday_calendars", "status", name="ix_holiday_calendars_org_status"),
    )

    organization_id: Mapped[uuid.UUID] = _organization_column()
    calendar_code: Mapped[str] = mapped_column(String(30), nullable=False)
    calendar_code_normalized: Mapped[str] = mapped_column(
        String(30), Computed("UPPER(calendar_code)", persisted=True), nullable=False
    )
    calendar_name: Mapped[str] = mapped_column(String(150), nullable=False)
    is_organization_default: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    status: Mapped[str] = mapped_column(
        String(10), nullable=False, default="ACTIVE", server_default="ACTIVE"
    )
    active_default_key: Mapped[int | None] = mapped_column(
        SmallInteger,
        Computed(
            "CASE WHEN is_organization_default = 1 AND status = 'ACTIVE' THEN 1 END",
            persisted=True,
        ),
        nullable=True,
    )


class Holiday(Base, StandardColumns):
    """One holiday date in a calendar (§5.2). Cancelled, never deleted."""

    __tablename__ = "holidays"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_holidays_org_id"),
        ForeignKeyConstraint(
            ["organization_id", "calendar_id"],
            ["holiday_calendars.organization_id", "holiday_calendars.id"],
            name="fk_holidays_calendar",
            ondelete="RESTRICT",
        ),
        # (calendar, date) is unique among ACTIVE rows only: a cancelled date can be re-added.
        UniqueConstraint("calendar_id", "active_date_key", name="uq_holidays_calendar_active_date"),
        CheckConstraint("CHAR_LENGTH(holiday_name) BETWEEN 2 AND 150", name="ck_holidays_name"),
        CheckConstraint(_in("holiday_type", HolidayType), name="ck_holidays_type"),
        CheckConstraint(_in("status", HolidayStatus), name="ck_holidays_status"),
        CheckConstraint(
            "(status = 'CANCELLED') = (cancellation_reason IS NOT NULL) AND "
            "(cancellation_reason IS NULL OR CHAR_LENGTH(cancellation_reason) >= 10)",
            name="ck_holidays_cancellation_reason",
        ),
        archive_reason_constraint("holidays"),
        tenant_index(
            "holidays", "calendar_id", "holiday_date", name="ix_holidays_org_calendar_date"
        ),
        tenant_index("holidays", "holiday_date", name="ix_holidays_org_date"),
    )

    organization_id: Mapped[uuid.UUID] = _organization_column()
    calendar_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    holiday_date: Mapped[date] = mapped_column(Date, nullable=False)
    holiday_name: Mapped[str] = mapped_column(String(150), nullable=False)
    holiday_type: Mapped[str] = mapped_column(String(10), nullable=False)
    status: Mapped[str] = mapped_column(
        String(10), nullable=False, default="ACTIVE", server_default="ACTIVE"
    )
    cancellation_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    active_date_key: Mapped[date | None] = mapped_column(
        Date,
        Computed("CASE WHEN status = 'ACTIVE' THEN holiday_date END", persisted=True),
        nullable=True,
    )


class BranchHolidayCalendarAssignment(Base, StandardColumns):
    """The calendar a branch follows over an interval ``[effective_from, effective_to)``.

    Non-overlap per branch is a service rule (module docstring); the unique key below only
    stops two assignments from starting on the same day.
    """

    __tablename__ = "branch_holiday_calendar_assignments"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_branch_calendar_assignments_org_id"),
        UniqueConstraint(
            "branch_id", "effective_from", name="uq_branch_calendar_assignments_branch_start"
        ),
        ForeignKeyConstraint(
            ["organization_id", "branch_id"],
            ["branches.organization_id", "branches.id"],
            name="fk_branch_calendar_assignments_branch",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "calendar_id"],
            ["holiday_calendars.organization_id", "holiday_calendars.id"],
            name="fk_branch_calendar_assignments_calendar",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from",
            name="ck_branch_calendar_assignments_range",
        ),
        archive_reason_constraint("branch_holiday_calendar_assignments"),
        tenant_index(
            "branch_holiday_calendar_assignments",
            "branch_id",
            "effective_from",
            name="ix_branch_calendar_assignments_org_branch",
        ),
    )

    organization_id: Mapped[uuid.UUID] = _organization_column()
    branch_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    calendar_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)


# ======================================================================================
# Attendance core (5.3-5.9, 5.13): shifts, policies, assignments, punches, records.
# Derived columns of the record (5.9 "DER (stored)") are plain columns written by the
# evaluation service in the same transaction as the punches they derive from.
# ======================================================================================


def _org_fk(table: str, column: str, name: str) -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["organization_id", column],
        [f"{table}.organization_id", f"{table}.id"],
        name=name,
        ondelete="RESTRICT",
    )


class Shift(Base, StandardColumns):
    __tablename__ = "shifts"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_shifts_org_id"),
        UniqueConstraint("organization_id", "shift_code_normalized", name="uq_shifts_code"),
        CheckConstraint(f"shift_code REGEXP {CODE_PATTERN_SQL}", name="ck_shifts_code"),
        CheckConstraint("CHAR_LENGTH(shift_name) BETWEEN 2 AND 100", name="ck_shifts_name"),
        CheckConstraint("end_time <> start_time", name="ck_shifts_times"),
        CheckConstraint(
            "break_minutes >= 0 AND break_minutes < duration_minutes", name="ck_shifts_break"
        ),
        CheckConstraint(
            "half_day_min_work_minutes >= 1 "
            "AND half_day_min_work_minutes < full_day_min_work_minutes "
            "AND full_day_min_work_minutes <= duration_minutes",
            name="ck_shifts_thresholds",
        ),
        CheckConstraint(
            "window_before_minutes BETWEEN 0 AND 720 AND window_after_minutes BETWEEN 0 AND 720 "
            "AND duration_minutes + window_before_minutes + window_after_minutes <= 1440",
            name="ck_shifts_windows",
        ),
        CheckConstraint("status IN ('ACTIVE','INACTIVE')", name="ck_shifts_status"),
        archive_reason_constraint("shifts"),
        tenant_index("shifts", "status", name="ix_shifts_org_status"),
    )

    organization_id: Mapped[uuid.UUID] = _organization_column()
    shift_code: Mapped[str] = mapped_column(String(30), nullable=False)
    shift_code_normalized: Mapped[str] = mapped_column(
        String(30), Computed("UPPER(shift_code)", persisted=True), nullable=False
    )
    shift_name: Mapped[str] = mapped_column(String(100), nullable=False)
    start_time: Mapped[time] = mapped_column(Time, nullable=False)
    end_time: Mapped[time] = mapped_column(Time, nullable=False)
    is_overnight: Mapped[bool] = mapped_column(
        Boolean, Computed("end_time < start_time", persisted=True), nullable=False
    )
    duration_minutes: Mapped[int] = mapped_column(
        SmallInteger,
        Computed(
            "MOD(TIME_TO_SEC(end_time) - TIME_TO_SEC(start_time) + 86400, 86400) DIV 60",
            persisted=True,
        ),
        nullable=False,
    )
    break_minutes: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    full_day_min_work_minutes: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    half_day_min_work_minutes: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    window_before_minutes: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=120)
    window_after_minutes: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=240)
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="ACTIVE")


class AttendancePolicy(Base, StandardColumns):
    __tablename__ = "attendance_policies"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_attendance_policies_org_id"),
        UniqueConstraint(
            "organization_id", "policy_code_normalized", name="uq_attendance_policies_code"
        ),
        CheckConstraint(
            f"policy_code REGEXP {CODE_PATTERN_SQL}", name="ck_attendance_policies_code"
        ),
        CheckConstraint("status IN ('ACTIVE','INACTIVE')", name="ck_attendance_policies_status"),
        archive_reason_constraint("attendance_policies"),
        tenant_index("attendance_policies", "status", name="ix_attendance_policies_status"),
    )

    organization_id: Mapped[uuid.UUID] = _organization_column()
    policy_code: Mapped[str] = mapped_column(String(30), nullable=False)
    policy_code_normalized: Mapped[str] = mapped_column(
        String(30), Computed("UPPER(policy_code)", persisted=True), nullable=False
    )
    policy_name: Mapped[str] = mapped_column(String(150), nullable=False)
    description: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="ACTIVE")


_ATT_APPLIES = (
    ("branch", "branches"),
    ("department", "departments"),
    ("work_location", "work_locations"),
    ("employment_type", "employment_types"),
    ("employee_category", "employee_categories"),
)
_ATT_SPECIFICITY_SQL = " + ".join(f"(applies_{name}_id IS NOT NULL)" for name, _ in _ATT_APPLIES)


class AttendancePolicyVersion(Base, StandardColumns):
    __tablename__ = "attendance_policy_versions"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_attendance_policy_versions_org_id"),
        UniqueConstraint(
            "policy_id", "version_number", name="uq_attendance_policy_versions_number"
        ),
        UniqueConstraint("policy_id", "draft_key", name="uq_attendance_policy_versions_one_draft"),
        _org_fk("attendance_policies", "policy_id", "fk_att_policy_versions_policy"),
        _org_fk("shifts", "default_shift_id", "fk_att_policy_versions_shift"),
        _org_fk(
            "weekly_off_patterns", "default_weekly_off_pattern_id", "fk_att_policy_versions_pattern"
        ),
        *(
            _org_fk(table, f"applies_{name}_id", f"fk_att_policy_versions_{name}")
            for name, table in _ATT_APPLIES
        ),
        CheckConstraint(
            "status IN ('DRAFT','ACTIVE','ARCHIVED')", name="ck_att_policy_versions_status"
        ),
        CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from",
            name="ck_att_policy_versions_range",
        ),
        CheckConstraint(
            "status = 'DRAFT' OR effective_from IS NOT NULL",
            name="ck_att_policy_versions_published",
        ),
        CheckConstraint("priority BETWEEN 1 AND 999", name="ck_att_policy_versions_priority"),
        CheckConstraint(
            "punch_evaluation_mode IN ('FIRST_IN_LAST_OUT','PAIRED_PUNCHES')",
            name="ck_att_policy_versions_mode",
        ),
        CheckConstraint(
            "break_deduction_mode IN ('NONE','FIXED_FROM_SHIFT')",
            name="ck_att_policy_versions_break",
        ),
        CheckConstraint(
            "break_deduction_mode = 'NONE' OR punch_evaluation_mode = 'FIRST_IN_LAST_OUT'",
            name="ck_att_policy_versions_break_mode",
        ),
        CheckConstraint(
            "regularization_approval_chain IN ('MANAGER_ONLY','HR_ONLY','MANAGER_THEN_HR')",
            name="ck_att_policy_versions_chain",
        ),
        CheckConstraint(
            "incomplete_on_finalize IN ('BLOCK','MARK_ABSENT')",
            name="ck_att_policy_versions_incomplete",
        ),
        CheckConstraint(
            "late_grace_minutes BETWEEN 0 AND 240 AND "
            "early_departure_grace_minutes BETWEEN 0 AND 240",
            name="ck_att_policy_versions_grace",
        ),
        CheckConstraint(
            "duplicate_punch_window_seconds BETWEEN 0 AND 600",
            name="ck_att_policy_versions_duplicate",
        ),
        CheckConstraint(
            "regularization_backdate_limit_days BETWEEN 0 AND 90",
            name="ck_att_policy_versions_backdate",
        ),
        CheckConstraint(
            "auto_finalize_after_days IS NULL OR auto_finalize_after_days BETWEEN 1 AND 90",
            name="ck_att_policy_versions_auto_finalize",
        ),
        CheckConstraint(
            "status = 'DRAFT' OR default_shift_id IS NOT NULL OR "
            "(flexible_full_day_min_work_minutes BETWEEN 1 AND 1440 "
            "AND flexible_half_day_min_work_minutes >= 1 "
            "AND flexible_half_day_min_work_minutes < flexible_full_day_min_work_minutes)",
            name="ck_att_policy_versions_flexible",
        ),
        tenant_index(
            "attendance_policy_versions",
            "status",
            "effective_from",
            name="ix_att_policy_versions_resolution",
        ),
    )

    organization_id: Mapped[uuid.UUID] = _organization_column()
    policy_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="DRAFT")
    draft_key: Mapped[int | None] = mapped_column(
        SmallInteger,
        Computed("CASE WHEN status = 'DRAFT' THEN 1 END", persisted=True),
        nullable=True,
    )
    effective_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    priority: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=100)
    applies_branch_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    applies_department_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    applies_work_location_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    applies_employment_type_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    applies_employee_category_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    applicability_specificity: Mapped[int] = mapped_column(
        SmallInteger, Computed(_ATT_SPECIFICITY_SQL, persisted=True), nullable=False
    )
    timezone: Mapped[str] = mapped_column(String(50), nullable=False)
    default_shift_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    default_weekly_off_pattern_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    punch_evaluation_mode: Mapped[str] = mapped_column(
        String(20), nullable=False, default="FIRST_IN_LAST_OUT"
    )
    break_deduction_mode: Mapped[str] = mapped_column(String(20), nullable=False, default="NONE")
    late_grace_minutes: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    early_departure_grace_minutes: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=0
    )
    flexible_full_day_min_work_minutes: Mapped[int | None] = mapped_column(
        SmallInteger, nullable=True
    )
    flexible_half_day_min_work_minutes: Mapped[int | None] = mapped_column(
        SmallInteger, nullable=True
    )
    flexible_day_start_time: Mapped[time] = mapped_column(Time, nullable=False, default=time(0, 0))
    self_punch_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    allow_remote_self_punch: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    manual_punch_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    integration_punch_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    duplicate_punch_window_seconds: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=60
    )
    regularization_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    regularization_backdate_limit_days: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=7
    )
    on_duty_requests_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    regularization_approval_chain: Mapped[str] = mapped_column(
        String(20), nullable=False, default="MANAGER_ONLY"
    )
    auto_finalize_after_days: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    incomplete_on_finalize: Mapped[str] = mapped_column(String(12), nullable=False, default="BLOCK")
    change_note: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    published_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid7, ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    published_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)


class EmployeeShiftAssignment(Base, StandardColumns):
    __tablename__ = "employee_shift_assignments"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_employee_shift_assignments_org_id"),
        UniqueConstraint(
            "employee_id", "effective_from", name="uq_employee_shift_assignments_start"
        ),
        _org_fk("employees", "employee_id", "fk_shift_assignments_employee"),
        _org_fk("shifts", "shift_id", "fk_shift_assignments_shift"),
        _org_fk("weekly_off_patterns", "weekly_off_pattern_id", "fk_shift_assignments_pattern"),
        CheckConstraint(
            "shift_id IS NOT NULL OR weekly_off_pattern_id IS NOT NULL",
            name="ck_shift_assignments_target",
        ),
        CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from",
            name="ck_shift_assignments_range",
        ),
        archive_reason_constraint("employee_shift_assignments"),
        tenant_index(
            "employee_shift_assignments",
            "employee_id",
            "effective_from",
            name="ix_shift_assignments_emp",
        ),
    )

    organization_id: Mapped[uuid.UUID] = _organization_column()
    employee_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    shift_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    weekly_off_pattern_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    reason: Mapped[str | None] = mapped_column(String(500), nullable=True)


class AttendanceRecord(Base, StandardColumns):
    """One employee's attendance outcome for one business date (5.9). Never deleted."""

    __tablename__ = "attendance_records"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_attendance_records_org_id"),
        UniqueConstraint(
            "organization_id", "employee_id", "business_date", name="uq_attendance_records_day"
        ),
        _org_fk("employees", "employee_id", "fk_attendance_records_employee"),
        _org_fk(
            "attendance_policy_versions",
            "attendance_policy_version_id",
            "fk_attendance_records_version",
        ),
        _org_fk("shifts", "shift_id", "fk_attendance_records_shift"),
        _org_fk(
            "employee_shift_assignments", "shift_assignment_id", "fk_attendance_records_assignment"
        ),
        _org_fk("holidays", "holiday_id", "fk_attendance_records_holiday"),
        ForeignKeyConstraint(
            ["employee_id", "first_half_leave_application_id"],
            ["leave_applications.employee_id", "leave_applications.id"],
            name="fk_attendance_records_first_leave",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["employee_id", "second_half_leave_application_id"],
            ["leave_applications.employee_id", "leave_applications.id"],
            name="fk_attendance_records_second_leave",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "day_type IN ('WORKING_DAY','WEEKLY_OFF','HOLIDAY')",
            name="ck_attendance_records_day_type",
        ),
        CheckConstraint(
            "first_half_status IS NULL OR first_half_status IN "
            "('PRESENT','ABSENT','ON_LEAVE','WEEKLY_OFF','HOLIDAY')",
            name="ck_attendance_records_first_half",
        ),
        CheckConstraint(
            "second_half_status IS NULL OR second_half_status IN "
            "('PRESENT','ABSENT','ON_LEAVE','WEEKLY_OFF','HOLIDAY')",
            name="ck_attendance_records_second_half",
        ),
        CheckConstraint(
            "first_half_source IN ('EVALUATED','LEAVE','OVERRIDE','ON_DUTY') "
            "AND second_half_source IN ('EVALUATED','LEAVE','OVERRIDE','ON_DUTY')",
            name="ck_attendance_records_sources",
        ),
        CheckConstraint(
            "(first_half_source = 'LEAVE') = (first_half_leave_application_id IS NOT NULL) "
            "AND (second_half_source = 'LEAVE') = (second_half_leave_application_id IS NOT NULL)",
            name="ck_attendance_records_leave_link",
        ),
        CheckConstraint(
            "day_status IN "
            "('PRESENT','ABSENT','ON_LEAVE','WEEKLY_OFF','HOLIDAY','SPLIT','INCOMPLETE')",
            name="ck_attendance_records_day_status",
        ),
        CheckConstraint(
            "work_arrangement IN ('OFFICE','REMOTE','ON_DUTY','UNSPECIFIED')",
            name="ck_attendance_records_arrangement",
        ),
        CheckConstraint("record_state IN ('OPEN','FINALIZED')", name="ck_attendance_records_state"),
        tenant_index(
            "attendance_records",
            "business_date",
            "day_status",
            name="ix_attendance_records_date_status",
        ),
        tenant_index(
            "attendance_records",
            "employee_id",
            "business_date",
            name="ix_attendance_records_employee",
        ),
        tenant_index(
            "attendance_records",
            "record_state",
            "business_date",
            name="ix_attendance_records_state",
        ),
    )

    organization_id: Mapped[uuid.UUID] = _organization_column()
    employee_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    business_date: Mapped[date] = mapped_column(Date, nullable=False)
    attendance_policy_version_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    timezone_snapshot: Mapped[str] = mapped_column(String(50), nullable=False)
    shift_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    shift_assignment_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    shift_code_snapshot: Mapped[str | None] = mapped_column(String(30), nullable=True)
    scheduled_start_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    scheduled_end_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    scheduled_work_minutes: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    weekly_off_pattern_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    holiday_calendar_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    day_type: Mapped[str] = mapped_column(String(12), nullable=False)
    holiday_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    first_in_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    last_out_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    gross_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    break_deducted_minutes: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    worked_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    late_by_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    early_by_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_late: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_early_departure: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    has_missing_punch: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    has_out_of_window_punch: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    has_held_punch: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    worked_on_non_working_day: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    punch_on_leave_conflict: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    first_half_status: Mapped[str | None] = mapped_column(String(10), nullable=True)
    second_half_status: Mapped[str | None] = mapped_column(String(10), nullable=True)
    first_half_source: Mapped[str] = mapped_column(String(10), nullable=False, default="EVALUATED")
    second_half_source: Mapped[str] = mapped_column(String(10), nullable=False, default="EVALUATED")
    first_half_leave_application_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    second_half_leave_application_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    day_status: Mapped[str] = mapped_column(String(10), nullable=False, default="INCOMPLETE")
    work_arrangement: Mapped[str] = mapped_column(String(12), nullable=False, default="UNSPECIFIED")
    record_state: Mapped[str] = mapped_column(String(10), nullable=False, default="OPEN")
    finalized_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    finalized_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid7, ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    finalization_mode: Mapped[str | None] = mapped_column(String(10), nullable=True)
    auto_marked_absent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    has_pending_regularization: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    post_finalization_change: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    day_close_processed_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    evaluation_version: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1)
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    remarks: Mapped[str | None] = mapped_column(String(500), nullable=True)


class AttendancePunch(Base, StandardColumns):
    """A raw time event (5.7). Never edited or deleted; corrections void and add (ATT-12)."""

    __tablename__ = "attendance_punches"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_attendance_punches_org_id"),
        # Replay detection (ATT-13); NULLs of non-integration rows never collide.
        UniqueConstraint(
            "organization_id",
            "integration_source_code",
            "external_event_id",
            name="uq_attendance_punches_external_event",
        ),
        _org_fk("employees", "employee_id", "fk_attendance_punches_employee"),
        _org_fk("attendance_records", "attendance_record_id", "fk_attendance_punches_record"),
        CheckConstraint("punch_type IN ('IN','OUT')", name="ck_attendance_punches_type"),
        CheckConstraint(
            "source IN ('SELF_SERVICE','MANUAL','INTEGRATION','REGULARIZATION')",
            name="ck_attendance_punches_source",
        ),
        CheckConstraint(
            "work_arrangement IS NULL OR work_arrangement IN ('OFFICE','REMOTE')",
            name="ck_attendance_punches_arrangement",
        ),
        CheckConstraint(
            "punch_status IN ('VALID','DUPLICATE','OUT_OF_WINDOW','HELD','VOIDED')",
            name="ck_attendance_punches_status",
        ),
        CheckConstraint(
            "(source = 'INTEGRATION') = (integration_source_code IS "
            "NOT NULL AND external_event_id IS NOT NULL)",
            name="ck_attendance_punches_integration",
        ),
        CheckConstraint(
            "source <> 'MANUAL' OR (remarks IS NOT NULL AND CHAR_LENGTH(remarks) >= 10)",
            name="ck_attendance_punches_manual_remarks",
        ),
        CheckConstraint(
            "(punch_status = 'VOIDED') = (voided_at IS NOT NULL)",
            name="ck_attendance_punches_voided",
        ),
        tenant_index(
            "attendance_punches", "employee_id", "punch_at", name="ix_attendance_punches_emp"
        ),
        Index("ix_attendance_punches_record", "attendance_record_id", "punch_status"),
    )

    organization_id: Mapped[uuid.UUID] = _organization_column()
    employee_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    punch_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    punch_type: Mapped[str] = mapped_column(String(3), nullable=False)
    source: Mapped[str] = mapped_column(String(14), nullable=False)
    work_arrangement: Mapped[str | None] = mapped_column(String(6), nullable=True)
    integration_source_code: Mapped[str | None] = mapped_column(String(30), nullable=True)
    external_event_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    business_date: Mapped[date] = mapped_column(Date, nullable=False)
    attendance_record_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    punch_status: Mapped[str] = mapped_column(String(14), nullable=False, default="VALID")
    duplicate_of_punch_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    regularization_request_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    remarks: Mapped[str | None] = mapped_column(String(500), nullable=True)
    voided_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    voided_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid7, ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    void_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    void_source: Mapped[str | None] = mapped_column(String(20), nullable=True)


class AttendanceRecordRevision(Base, AppendOnlyColumns):
    """Append-only history of a record (5.13): before and after snapshots."""

    __tablename__ = "attendance_record_revisions"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_attendance_record_revisions_org_id"),
        UniqueConstraint(
            "attendance_record_id", "revision_no", name="uq_attendance_record_revisions_number"
        ),
        _org_fk("employees", "employee_id", "fk_attendance_revisions_employee"),
        _org_fk("attendance_records", "attendance_record_id", "fk_attendance_revisions_record"),
        CheckConstraint(
            "change_source IN ('MANUAL_PUNCH','PUNCH_VOIDED','HELD_PUNCH_APPLIED',"
            "'REGULARIZATION_APPROVED','ADMIN_CORRECTION','LEAVE_APPROVED','LEAVE_CANCELLED',"
            "'FINALIZED','REOPENED','SCHEDULE_RECALCULATED','HOLIDAY_RECALCULATED')",
            name="ck_attendance_revisions_source",
        ),
        Index("ix_attendance_revisions_record", "attendance_record_id", "occurred_at"),
    )

    organization_id: Mapped[uuid.UUID] = _organization_column()
    employee_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    attendance_record_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False)
    change_source: Mapped[str] = mapped_column(String(30), nullable=False)
    before_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    after_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    related_punch_ids: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    related_regularization_request_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid7, nullable=True
    )
    related_leave_application_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    reason: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    actor_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid7, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    occurred_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)


# ======================================================================================
# Regularization (5.14): requests, immutable items, append-only actions.
# ======================================================================================


class AttendanceRegularizationRequest(Base, StandardColumns):
    __tablename__ = "attendance_regularization_requests"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_att_reg_requests_org_id"),
        # One SUBMITTED request per record (partial unique via a generated key).
        UniqueConstraint("attendance_record_id", "pending_key", name="uq_att_reg_requests_pending"),
        _org_fk("employees", "employee_id", "fk_att_reg_requests_employee"),
        _org_fk("attendance_records", "attendance_record_id", "fk_att_reg_requests_record"),
        _org_fk("employee_documents", "supporting_document_id", "fk_att_reg_requests_document"),
        CheckConstraint(
            "request_type IN ('MISSING_PUNCH','PUNCH_CORRECTION','ON_DUTY')",
            name="ck_att_reg_requests_type",
        ),
        CheckConstraint(
            "status IN ('SUBMITTED','APPROVED','REJECTED','WITHDRAWN','CANCELLED')",
            name="ck_att_reg_requests_status",
        ),
        CheckConstraint(
            "CHAR_LENGTH(reason) BETWEEN 10 AND 1000", name="ck_att_reg_requests_reason"
        ),
        CheckConstraint(
            "current_approver_type IS NULL OR current_approver_type IN ('MANAGER','HR_QUEUE')",
            name="ck_att_reg_requests_approver",
        ),
        tenant_index(
            "attendance_regularization_requests",
            "status",
            "current_approver_user_id",
            name="ix_att_reg_requests_approver",
        ),
        tenant_index(
            "attendance_regularization_requests",
            "employee_id",
            "business_date",
            name="ix_att_reg_requests_employee",
        ),
    )

    organization_id: Mapped[uuid.UUID] = _organization_column()
    employee_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    attendance_record_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    business_date: Mapped[date] = mapped_column(Date, nullable=False)
    request_type: Mapped[str] = mapped_column(String(20), nullable=False)
    reason: Mapped[str] = mapped_column(String(1000), nullable=False)
    supporting_document_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="SUBMITTED")
    pending_key: Mapped[int | None] = mapped_column(
        SmallInteger,
        Computed("CASE WHEN status = 'SUBMITTED' THEN 1 END", persisted=True),
        nullable=True,
    )
    approval_chain_snapshot: Mapped[str] = mapped_column(String(20), nullable=False)
    current_approval_level: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    current_approver_type: Mapped[str | None] = mapped_column(String(10), nullable=True)
    current_approver_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid7, ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    current_approver_employee_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    submitted_by: Mapped[uuid.UUID] = mapped_column(
        Uuid7, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    submitted_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    submitted_on_behalf: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    decided_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid7, ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    decided_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    decision_comment: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    resubmission_of_request_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    cancellation_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)


class AttendanceRegularizationItem(Base, AppendOnlyColumns):
    """Immutable after submission (5.14)."""

    __tablename__ = "attendance_regularization_items"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_att_reg_items_org_id"),
        _org_fk("attendance_regularization_requests", "request_id", "fk_att_reg_items_request"),
        CheckConstraint(
            "item_type IN ('ADD_PUNCH','VOID_PUNCH','SET_ON_DUTY')", name="ck_att_reg_items_type"
        ),
        CheckConstraint(
            "(item_type = 'ADD_PUNCH') = (punch_type IS NOT NULL AND punch_at IS NOT NULL)",
            name="ck_att_reg_items_add",
        ),
        CheckConstraint(
            "(item_type = 'VOID_PUNCH') = (target_punch_id IS NOT NULL)",
            name="ck_att_reg_items_void",
        ),
        CheckConstraint(
            "(item_type = 'SET_ON_DUTY') = (half IS NOT NULL)", name="ck_att_reg_items_duty"
        ),
        Index("ix_att_reg_items_request", "request_id"),
    )

    organization_id: Mapped[uuid.UUID] = _organization_column()
    request_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    item_type: Mapped[str] = mapped_column(String(12), nullable=False)
    punch_type: Mapped[str | None] = mapped_column(String(3), nullable=True)
    punch_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    work_arrangement: Mapped[str | None] = mapped_column(String(6), nullable=True)
    target_punch_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    half: Mapped[str | None] = mapped_column(String(11), nullable=True)
    created_punch_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)


class AttendanceRegularizationAction(Base, AppendOnlyColumns):
    __tablename__ = "attendance_regularization_actions"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_att_reg_actions_org_id"),
        _org_fk("attendance_regularization_requests", "request_id", "fk_att_reg_actions_request"),
        CheckConstraint(
            "action IN ('SUBMITTED','APPROVED_LEVEL_1','APPROVED_FINAL','OVERRIDE_APPROVED',"
            "'REJECTED','WITHDRAWN','REASSIGNED','CANCELLED')",
            name="ck_att_reg_actions_action",
        ),
        Index("ix_att_reg_actions_request", "request_id", "occurred_at"),
    )

    organization_id: Mapped[uuid.UUID] = _organization_column()
    request_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    action: Mapped[str] = mapped_column(String(20), nullable=False)
    approval_level: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    from_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    to_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    from_approver_type: Mapped[str | None] = mapped_column(String(10), nullable=True)
    to_approver_type: Mapped[str | None] = mapped_column(String(10), nullable=True)
    from_approver_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    to_approver_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    comment: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    actor_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid7, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    actor_employee_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)


__all__ = [
    "AttendancePolicy",
    "AttendancePolicyVersion",
    "AttendancePunch",
    "AttendanceRecord",
    "AttendanceRecordRevision",
    "AttendanceRegularizationAction",
    "AttendanceRegularizationItem",
    "AttendanceRegularizationRequest",
    "BranchHolidayCalendarAssignment",
    "EmployeeShiftAssignment",
    "Holiday",
    "HolidayCalendar",
    "HolidayStatus",
    "HolidayType",
    "Shift",
    "WeeklyOffOccurrence",
    "WeeklyOffPattern",
    "WeeklyOffPatternDay",
]
