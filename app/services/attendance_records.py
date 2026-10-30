"""Attendance record engine: creation, evaluation, revisions and the leave effects (5.9-5.13, 7.4).

Callers hold the employee row lock (7.3 lock order: employee, ..., leave application, then
attendance records ascending by business date). Nothing here commits.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterable
from datetime import date, datetime, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from app.core.clock import utc_now
from app.core.errors import LeaveConflictsWithAttendanceOverride
from app.db.types import uuid7
from app.models.attendance import (
    AttendancePolicyVersion,
    AttendancePunch,
    AttendanceRecord,
    AttendanceRecordRevision,
    Shift,
)
from app.models.leave import LeaveApplication
from app.repositories.attendance import (
    AttendancePolicyVersionRepository,
    AttendanceRecordRepository,
    PunchRepository,
    RevisionRepository,
    ShiftRepository,
)
from app.services.attendance_rules import (
    EVALUATION_VERSION,
    FIRST_HALF,
    SECOND_HALF,
    DayWindow,
    FixedHalf,
    PolicyTimes,
    PunchIn,
    Result,
    ShiftTimes,
    day_status,
    evaluate,
    window_closed,
)
from app.services.schedule_resolver import Schedule

HALVES = (FIRST_HALF, SECOND_HALF)


class SlotLike(Protocol):
    """What the leave hooks read from a slot: a persisted slot or a freshly evaluated one."""

    @property
    def slot_date(self) -> date: ...

    @property
    def half(self) -> str: ...

    @property
    def counted(self) -> bool: ...


_SNAPSHOT_FIELDS = (
    "record_state",
    "day_type",
    "holiday_id",
    "first_half_status",
    "second_half_status",
    "first_half_source",
    "second_half_source",
    "first_half_leave_application_id",
    "second_half_leave_application_id",
    "first_in_at",
    "last_out_at",
    "worked_minutes",
    "late_by_minutes",
    "early_by_minutes",
    "is_late",
    "is_early_departure",
    "has_missing_punch",
    "has_out_of_window_punch",
    "has_held_punch",
    "punch_on_leave_conflict",
    "auto_marked_absent",
    "work_arrangement",
    "day_status",
)


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    return value


def snapshot(record: AttendanceRecord) -> dict[str, Any]:
    """The 5.13 before/after snapshot of a record."""
    return {name: _jsonable(getattr(record, name)) for name in _SNAPSHOT_FIELDS}


def day_window(schedule: Schedule, organization_tz: ZoneInfo) -> DayWindow | None:
    """How punches map to a business date under this schedule (None without a policy)."""
    version = schedule.policy_version
    if version is None:
        return None
    zone = ZoneInfo(version.timezone or organization_tz.key)
    shift = schedule.shift
    if shift is None:
        return DayWindow(tz=zone, flexible_day_start=version.flexible_day_start_time)
    return DayWindow(
        tz=zone,
        flexible_day_start=version.flexible_day_start_time,
        shift_start=shift.start_time,
        shift_end=shift.end_time,
        window_before=shift.window_before_minutes,
        window_after=shift.window_after_minutes,
    )


def policy_times(version: AttendancePolicyVersion) -> PolicyTimes:
    return PolicyTimes(
        evaluation_mode=version.punch_evaluation_mode,
        break_mode=version.break_deduction_mode,
        late_grace=version.late_grace_minutes,
        early_grace=version.early_departure_grace_minutes,
        flexible_full_day_min=version.flexible_full_day_min_work_minutes or 480,
        flexible_half_day_min=version.flexible_half_day_min_work_minutes or 240,
    )


class RecordEngine:
    def __init__(
        self,
        *,
        records: AttendanceRecordRepository,
        punches: PunchRepository,
        revisions: RevisionRepository,
        shifts: ShiftRepository,
        versions: AttendancePolicyVersionRepository,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._records = records
        self._punches = punches
        self._revisions = revisions
        self._shifts = shifts
        self._versions = versions
        self._clock = clock

    # Creation.

    def get_or_create(
        self, employee_id: uuid.UUID, business_date: date, schedule: Schedule, actor_id: uuid.UUID
    ) -> AttendanceRecord:
        """Insert-if-absent, lock, and on creation snapshot SCHEDULE and apply leave (INT-11)."""
        existing = self._records.lock_find(employee_id, business_date)
        if existing is not None:
            return existing
        version = schedule.policy_version
        assert version is not None  # noqa: S101 - callers resolve a policy first (ATT-01 f)
        zone = ZoneInfo(version.timezone)
        values: dict[str, Any] = {
            "employee_id": employee_id,
            "business_date": business_date,
            "attendance_policy_version_id": version.id,
            "timezone_snapshot": version.timezone,
            "weekly_off_pattern_id": schedule.pattern_id,
            "holiday_calendar_id": schedule.calendar_id,
            "day_type": schedule.day_type,
            "holiday_id": schedule.holiday.id if schedule.holiday else None,
            "shift_assignment_id": schedule.assignment.id if schedule.assignment else None,
            "created_by": actor_id,
        }
        if schedule.shift is not None:
            window = day_window(schedule, zone)
            assert window is not None  # noqa: S101
            start, end = window.shift_bounds(business_date)
            values |= {
                "shift_id": schedule.shift.id,
                "shift_code_snapshot": schedule.shift.shift_code,
                "scheduled_start_at": start,
                "scheduled_end_at": end,
                "scheduled_work_minutes": schedule.shift.duration_minutes
                - schedule.shift.break_minutes,
            }
        self._records.insert_if_absent(values)
        record = self._records.lock_find(employee_id, business_date)
        assert record is not None  # noqa: S101 - just inserted under the employee lock
        leave = self._records.approved_leave_halves(employee_id, business_date)
        for half, application_id in leave.items():
            self._set_leave(record, half, application_id)
        self.evaluate_into(record)
        return record

    # Evaluation.

    def policy_version(self, record: AttendanceRecord) -> AttendancePolicyVersion:
        return self._versions.require(record.attendance_policy_version_id, resource="policy")

    def is_window_closed(self, record: AttendanceRecord) -> bool:
        """5.10 step 6: free halves are evaluated only after the attendance window closes."""
        shift = self._shifts.require(record.shift_id, resource="shift") if record.shift_id else None
        return window_closed(
            self._clock(),
            record.business_date,
            shift_end=record.scheduled_end_at,
            window_after_minutes=shift.window_after_minutes if shift else 0,
            tz=ZoneInfo(record.timezone_snapshot),
            flexible_day_start=self.policy_version(record).flexible_day_start_time,
        )

    def evaluate_into(self, record: AttendanceRecord, *, force_closed: bool = False) -> Result:
        """Run 5.10 and store the derived columns. ``force_closed`` is for FINALIZED records."""
        version = self.policy_version(record)
        shift: Shift | None = (
            self._shifts.require(record.shift_id, resource="shift") if record.shift_id else None
        )
        punches = [
            PunchIn(p.id, p.punch_at, p.punch_type, p.work_arrangement)
            for p in self._punches.valid_for_record(record.id)
        ]
        fixed: dict[str, FixedHalf] = {}
        for half in HALVES:
            source = getattr(record, f"{half.lower()}_source")
            status = getattr(record, f"{half.lower()}_status")
            if source != "EVALUATED" and status is not None:
                fixed[half] = FixedHalf(
                    status, source, getattr(record, f"{half.lower()}_leave_application_id")
                )
        shift_times = (
            ShiftTimes(
                record.scheduled_start_at,  # type: ignore[arg-type]
                record.scheduled_end_at,  # type: ignore[arg-type]
                shift.duration_minutes,
                shift.break_minutes,
                shift.full_day_min_work_minutes,
                shift.half_day_min_work_minutes,
                shift.window_after_minutes,
            )
            if shift is not None
            else None
        )
        closed = force_closed or self.is_window_closed(record)
        result = evaluate(
            punches,
            shift=shift_times,
            policy=policy_times(version),
            day_type=record.day_type,
            fixed=fixed,
            window_closed=closed,
        )
        self._store(record, result)
        return result

    def _store(self, record: AttendanceRecord, result: Result) -> None:
        record.first_in_at = result.first_in
        record.last_out_at = result.last_out
        record.gross_minutes = result.gross_minutes
        record.break_deducted_minutes = result.break_deducted
        record.worked_minutes = result.worked_minutes
        record.late_by_minutes = result.late_by
        record.early_by_minutes = result.early_by
        record.is_late = result.is_late
        record.is_early_departure = result.is_early
        record.has_missing_punch = result.has_missing_punch
        record.first_half_status = result.halves[FIRST_HALF]
        record.second_half_status = result.halves[SECOND_HALF]
        record.day_status = result.day_status
        record.work_arrangement = result.work_arrangement
        record.evaluation_version = EVALUATION_VERSION
        statuses = {p.punch_status for p in self._punches.for_record(record.id)}
        record.has_out_of_window_punch = "OUT_OF_WINDOW" in statuses
        record.has_held_punch = "HELD" in statuses
        record.worked_on_non_working_day = record.day_type != "WORKING_DAY" and bool(
            result.worked_minutes
        )
        has_leave = "ON_LEAVE" in (record.first_half_status, record.second_half_status)
        record.punch_on_leave_conflict = has_leave and "VALID" in statuses

    def valid_punches(self, record: AttendanceRecord) -> list[AttendancePunch]:
        return list(self._punches.valid_for_record(record.id))

    def midpoint(self, record: AttendanceRecord) -> datetime | None:
        """Shift midpoint (start + floor(duration / 2)); None for flexible records."""
        if record.shift_id is None or record.scheduled_start_at is None:
            return None
        shift = self._shifts.require(record.shift_id, resource="shift")
        return record.scheduled_start_at + timedelta(minutes=shift.duration_minutes // 2)

    # Halves.

    @staticmethod
    def _set_leave(record: AttendanceRecord, half: str, application_id: uuid.UUID) -> None:
        prefix = half.lower()
        setattr(record, f"{prefix}_status", "ON_LEAVE")
        setattr(record, f"{prefix}_source", "LEAVE")
        setattr(record, f"{prefix}_leave_application_id", application_id)

    @staticmethod
    def _clear_leave(record: AttendanceRecord, half: str, application_id: uuid.UUID) -> bool:
        prefix = half.lower()
        if getattr(record, f"{prefix}_leave_application_id") != application_id:
            return False
        setattr(record, f"{prefix}_status", None)
        setattr(record, f"{prefix}_source", "EVALUATED")
        setattr(record, f"{prefix}_leave_application_id", None)
        return True

    def try_finalize(
        self, record: AttendanceRecord, *, actor_user_id: uuid.UUID, mode: str
    ) -> str | None:
        """5.12: finalize an OPEN record, or return why it was skipped.

        The record is re-evaluated first: the window may have closed since the last punch.
        MARK_ABSENT turns undetermined halves into ABSENT (``auto_marked_absent``).
        """
        if not self.is_window_closed(record):
            return "WINDOW_OPEN"
        self.evaluate_into(record)
        if record.has_pending_regularization:
            return "PENDING_REGULARIZATION"
        policy = self.policy_version(record)
        before = snapshot(record)
        if record.day_status == "INCOMPLETE":
            if policy.incomplete_on_finalize != "MARK_ABSENT":
                return "INCOMPLETE"
            for half in HALVES:
                if getattr(record, f"{half.lower()}_status") is None:
                    setattr(record, f"{half.lower()}_status", "ABSENT")
            record.auto_marked_absent = True
            record.day_status = day_status(record.first_half_status, record.second_half_status)
        record.record_state = "FINALIZED"
        record.finalized_at = self._clock()
        record.finalized_by = actor_user_id
        record.finalization_mode = mode
        self.revise(record, before=before, change_source="FINALIZED", actor_user_id=actor_user_id)
        return None

    # Revisions.

    def revise(
        self,
        record: AttendanceRecord,
        *,
        before: dict[str, Any],
        change_source: str,
        actor_user_id: uuid.UUID,
        reason: str | None = None,
        punch_ids: Iterable[uuid.UUID] = (),
        leave_application_id: uuid.UUID | None = None,
        regularization_request_id: uuid.UUID | None = None,
    ) -> AttendanceRecordRevision:
        record.revision_no += 1
        if record.record_state == "FINALIZED" and change_source != "FINALIZED":
            record.post_finalization_change = True
        row = AttendanceRecordRevision(
            id=uuid7(),
            organization_id=record.organization_id,
            employee_id=record.employee_id,
            attendance_record_id=record.id,
            revision_no=record.revision_no,
            change_source=change_source,
            before_snapshot=before,
            after_snapshot=snapshot(record),
            related_punch_ids=[str(i) for i in punch_ids] or None,
            related_leave_application_id=leave_application_id,
            related_regularization_request_id=regularization_request_id,
            reason=reason,
            actor_user_id=actor_user_id,
            occurred_at=self._clock(),
            created_by=actor_user_id,
        )
        self._revisions.add(row)
        return row

    # Leave effects.

    def apply_leave(
        self,
        record: AttendanceRecord,
        halves: Iterable[str],
        application_id: uuid.UUID,
        *,
        actor_user_id: uuid.UUID,
    ) -> None:
        """INT-10: the counted halves become ON_LEAVE (source LEAVE); a LEAVE_APPROVED revision."""
        before = snapshot(record)
        for half in halves:
            self._set_leave(record, half, application_id)
        self.evaluate_into(record, force_closed=record.record_state == "FINALIZED")
        self.revise(
            record,
            before=before,
            change_source="LEAVE_APPROVED",
            actor_user_id=actor_user_id,
            leave_application_id=application_id,
        )

    def remove_leave(
        self,
        record: AttendanceRecord,
        halves: Iterable[str],
        application_id: uuid.UUID,
        *,
        actor_user_id: uuid.UUID,
    ) -> None:
        """INT-12: the half is re-evaluated from punches; a FINALIZED record stays complete."""
        before = snapshot(record)
        # A list, not a generator: ``any`` would stop after the first half and leave the other.
        cleared = [self._clear_leave(record, half, application_id) for half in halves]
        if not any(cleared):
            return
        finalized = record.record_state == "FINALIZED"
        self.evaluate_into(record, force_closed=finalized)
        if finalized:
            for half in HALVES:
                if getattr(record, f"{half.lower()}_status") is None:
                    setattr(record, f"{half.lower()}_status", "ABSENT")
                    record.auto_marked_absent = True
            record.day_status = day_status(record.first_half_status, record.second_half_status)
        self.revise(
            record,
            before=before,
            change_source="LEAVE_CANCELLED",
            actor_user_id=actor_user_id,
            leave_application_id=application_id,
        )


class AttendanceLeaveEffects:
    """What an approved or cancelled leave application does to attendance (INT-10..INT-13)."""

    def __init__(self, *, engine: RecordEngine, records: AttendanceRecordRepository) -> None:
        self._engine = engine
        self._records = records

    @staticmethod
    def _counted(slots: Iterable[SlotLike]) -> dict[date, list[str]]:
        by_date: dict[date, list[str]] = {}
        for slot in slots:
            if slot.counted:
                by_date.setdefault(slot.slot_date, []).append(slot.half)
        return by_date

    def punch_conflict(self, employee_id: uuid.UUID, slots: Iterable[SlotLike]) -> bool:
        """LEV-30: a counted half already holds VALID punches.

        For shift employees a punch belongs to the first half before the shift midpoint, else the
        second. For flexible employees the record has no midpoint, so any VALID punch on the date
        conflicts with both halves (an interpretation; recorded in DEC-OAL).
        """
        for day, halves in self._counted(slots).items():
            record = self._records.find(employee_id, day)
            if record is None:
                continue
            punches = self._engine.valid_punches(record)
            if not punches:
                continue
            midpoint = self._engine.midpoint(record)
            for punch in punches:
                half = FIRST_HALF if midpoint is None or punch.punch_at < midpoint else SECOND_HALF
                if midpoint is None or half in halves:
                    return True
        return False

    def assert_no_override_conflict(
        self, employee_id: uuid.UUID, slots: Iterable[SlotLike]
    ) -> None:
        """LEV-31: leave may not land on a half fixed by OVERRIDE or ON_DUTY."""
        for day, halves in self._counted(slots).items():
            record = self._records.find(employee_id, day)
            if record is None:
                continue
            for half in halves:
                if getattr(record, f"{half.lower()}_source") in ("OVERRIDE", "ON_DUTY"):
                    raise LeaveConflictsWithAttendanceOverride()

    def on_approved(
        self,
        employee_id: uuid.UUID,
        application: LeaveApplication,
        slots: Iterable[SlotLike],
        actor_user_id: uuid.UUID,
    ) -> None:
        for day, halves in sorted(self._counted(slots).items()):  # ascending (7.3)
            record = self._records.lock_find(employee_id, day)
            if record is not None:
                self._engine.apply_leave(
                    record, halves, application.id, actor_user_id=actor_user_id
                )

    def on_cancelled(
        self,
        employee_id: uuid.UUID,
        application: LeaveApplication,
        slots: Iterable[SlotLike],
        actor_user_id: uuid.UUID,
    ) -> None:
        for day, halves in sorted(self._counted(slots).items()):
            record = self._records.lock_find(employee_id, day)
            if record is not None:
                self._engine.remove_leave(
                    record, halves, application.id, actor_user_id=actor_user_id
                )
