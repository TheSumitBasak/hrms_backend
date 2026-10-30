"""ATTENDANCE_DAY_CLOSE and ATTENDANCE_AUTO_FINALIZE (addendum 5.12, 14.1).

Day close walks business dates ``max(today - 7, tracking start) .. today - 1`` and, per employee
in one transaction (7.3 lock order: employee, then records ascending), creates any missing record
for every eligible date and re-evaluates OPEN records whose window has closed. Records are
insert-if-absent and evaluation is deterministic, so a rerun, a manual run for the same date or
two overlapping runners cannot change the outcome (INT-14).
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.actions import AuditAction
from app.audit.writer import AuditWriter
from app.core.clock import utc_now
from app.core.errors import EmsError
from app.models.employee import Employee
from app.models.leave import BackgroundJobRun, JobCode
from app.repositories.attendance import (
    AttendancePolicyVersionRepository,
    AttendanceRecordRepository,
    PunchRepository,
    RevisionRepository,
    ShiftAssignmentRepository,
    ShiftRepository,
)
from app.repositories.branch import BranchRepository
from app.repositories.employee import EmployeeRepository
from app.repositories.organization import OrganizationRepository, OrganizationSettingsRepository
from app.repositories.schedule import (
    BranchCalendarAssignmentRepository,
    EmployeeBranchResolver,
    HolidayCalendarRepository,
    HolidayRepository,
    WeeklyOffPatternRepository,
)
from app.services.attendance_records import RecordEngine
from app.services.employee_status import attendance_eligible
from app.services.job_runs import JobBase, RunTally
from app.services.schedule import ScheduleService

CATCH_UP_DAYS = 7
MANUAL_WINDOW_DAYS = 31


class AttendanceJobs(JobBase):
    def __init__(self, session: Session, organization_id: uuid.UUID) -> None:
        super().__init__(session, organization_id)
        org = organization_id
        self._employees = EmployeeRepository(session, org)
        self._records = AttendanceRecordRepository(session, org)
        versions = AttendancePolicyVersionRepository(session, org)
        shifts = ShiftRepository(session, org)
        self._engine = RecordEngine(
            records=self._records,
            punches=PunchRepository(session, org),
            revisions=RevisionRepository(session, org),
            shifts=shifts,
            versions=versions,
        )
        self._schedule = ScheduleService(
            patterns=WeeklyOffPatternRepository(session, org),
            calendars=HolidayCalendarRepository(session, org),
            holidays=HolidayRepository(session, org),
            assignments=BranchCalendarAssignmentRepository(session, org),
            branches=BranchRepository(session, org),
            employees=self._employees,
            branch_resolver=EmployeeBranchResolver(session, org),
            organizations=OrganizationRepository(session),
            settings=OrganizationSettingsRepository(session),
            audit=AuditWriter(session),
            shift_assignments=ShiftAssignmentRepository(session, org),
            policy_versions=versions,
            shifts=shifts,
        )

    # Day close.

    def window(self, today: date) -> list[date]:
        """The dates a scheduled run covers: ``max(today - 7, tracking start) .. today - 1``."""
        start = today - timedelta(days=CATCH_UP_DAYS)
        tracking = self.settings.attendance_tracking_start_date
        if tracking is not None:
            start = max(start, tracking)
        return [start + timedelta(days=i) for i in range((today - start).days)]

    def run_day_close(
        self, dates: list[date], *, run_key: str, trigger: str, requested_by: uuid.UUID
    ) -> BackgroundJobRun:
        run = self.start_run(
            JobCode.ATTENDANCE_DAY_CLOSE,
            run_key=run_key,
            trigger=trigger,
            requested_by=requested_by,
            parameters={"dates": [d.isoformat() for d in dates]},
        )
        tally = RunTally()
        ids = list(
            self._session.execute(
                select(Employee.id)
                .where(
                    Employee.organization_id == self._org,
                    Employee.archived_at.is_(None),
                    Employee.joining_date.is_not(None),
                    Employee.status != "DRAFT",
                )
                .order_by(Employee.id)
            ).scalars()
        )
        self._session.rollback()  # end the read transaction before the per-employee ones
        for employee_id in ids:
            tally.processed += 1
            try:
                closed = self._close_employee(run, employee_id, dates)
                self._heartbeat(run)
                self._session.commit()
                tally.succeeded += 1 if closed else 0
                tally.skipped += 0 if closed else 1
            except EmsError as error:
                self._session.rollback()
                tally.failed += 1
                tally.failures.append(
                    {"employee_id": str(employee_id), "error_code": error.code.value}
                )
        run = self._session.merge(run)
        self._finish(run, tally, AuditAction.ATTENDANCE_DAY_CLOSE_COMPLETED)
        return run

    def _close_employee(
        self, run: BackgroundJobRun, employee_id: uuid.UUID, dates: list[date]
    ) -> int:
        """Create and evaluate the records of one employee. Returns how many were touched."""
        employee = self._employees.get_for_update(employee_id)
        if employee is None:
            return 0
        today = self.today()
        resolver = self._schedule.resolver(employee)
        touched = 0
        for day in sorted(dates):  # ascending business date (7.3)
            if not attendance_eligible(
                self._session,
                self._org,
                self.settings.attendance_tracking_start_date,
                employee,
                day,
                today=today,
            ):
                continue
            schedule = resolver.at(day)
            if schedule.policy_version is None:
                continue
            record = self._engine.get_or_create(employee.id, day, schedule, run.requested_by)
            if record.record_state != "OPEN":
                continue
            self._engine.evaluate_into(record)
            if self._engine.is_window_closed(record):
                record.day_close_processed_at = utc_now()
            touched += 1
        return touched

    # Auto-finalize.

    def run_auto_finalize(
        self, *, run_key: str, trigger: str, requested_by: uuid.UUID
    ) -> BackgroundJobRun:
        run = self.start_run(
            JobCode.ATTENDANCE_AUTO_FINALIZE,
            run_key=run_key,
            trigger=trigger,
            requested_by=requested_by,
            parameters=None,
        )
        tally = RunTally()
        today = self.today()
        candidates = self._records.open_before(today)
        pairs = [
            (r.employee_id, r.business_date, r.attendance_policy_version_id) for r in candidates
        ]
        self._session.rollback()
        policy_days = self._auto_finalize_days({v for _, _, v in pairs})
        for employee_id, day, version_id in pairs:
            after = policy_days.get(version_id)
            if after is None or day > today - timedelta(days=after):
                continue
            tally.processed += 1
            try:
                self._employees.get_for_update(employee_id)
                record = self._records.lock_find(employee_id, day)
                outcome = (
                    "gone"
                    if record is None or record.record_state != "OPEN"
                    else self._engine.try_finalize(
                        record, actor_user_id=run.requested_by, mode="AUTOMATIC"
                    )
                )
                self._heartbeat(run)
                self._session.commit()
                if outcome is None:
                    tally.succeeded += 1
                else:
                    tally.skipped += 1
            except EmsError as error:
                self._session.rollback()
                tally.failed += 1
                tally.failures.append(
                    {"employee_id": str(employee_id), "error_code": error.code.value}
                )
        run = self._session.merge(run)
        self._finish(run, tally, AuditAction.ATTENDANCE_DAY_CLOSE_COMPLETED)
        return run

    def _auto_finalize_days(self, version_ids: set[uuid.UUID]) -> dict[uuid.UUID, int | None]:
        versions = AttendancePolicyVersionRepository(self._session, self._org)
        return {
            vid: versions.require(vid, resource="policy").auto_finalize_after_days
            for vid in version_ids
        }
