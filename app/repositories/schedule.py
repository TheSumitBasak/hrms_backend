"""Work-schedule repositories (addendum §5.2)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date

from sqlalchemy import select

from app.models.attendance import (
    BranchHolidayCalendarAssignment,
    Holiday,
    HolidayCalendar,
    HolidayStatus,
    WeeklyOffPattern,
    WeeklyOffPatternDay,
)
from app.models.employee import EmployeeJobHistory
from app.repositories.base import OrgScopedRepository


class WeeklyOffPatternRepository(OrgScopedRepository[WeeklyOffPattern]):
    model = WeeklyOffPattern

    def list_patterns(self, *, status: str | None = None) -> Sequence[WeeklyOffPattern]:
        statement = self.select().order_by(WeeklyOffPattern.pattern_name, WeeklyOffPattern.id)
        if status is not None:
            statement = statement.where(WeeklyOffPattern.status == status)
        return list(self._session.execute(statement).scalars())

    def code_exists(self, code: str) -> bool:
        statement = (
            select(WeeklyOffPattern.id)
            .where(
                WeeklyOffPattern.organization_id == self._organization_id,
                WeeklyOffPattern.pattern_code_normalized == code.upper(),
            )
            .limit(1)
        )
        return self._session.execute(statement).first() is not None

    def days_for(
        self, pattern_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, list[WeeklyOffPatternDay]]:
        """Day rows for several patterns in one query (no N+1 on the list endpoint)."""
        grouped: dict[uuid.UUID, list[WeeklyOffPatternDay]] = {pid: [] for pid in pattern_ids}
        if not pattern_ids:
            return grouped
        statement = (
            select(WeeklyOffPatternDay)
            .where(
                WeeklyOffPatternDay.organization_id == self._organization_id,
                WeeklyOffPatternDay.pattern_id.in_(pattern_ids),
            )
            .order_by(WeeklyOffPatternDay.day_of_week, WeeklyOffPatternDay.occurrence)
        )
        for row in self._session.execute(statement).scalars():
            grouped[row.pattern_id].append(row)
        return grouped

    def replace_days(self, pattern_id: uuid.UUID, rows: Sequence[WeeklyOffPatternDay]) -> None:
        for existing in self.days_for([pattern_id])[pattern_id]:
            self._session.delete(existing)
        self._session.flush()
        self._session.add_all(rows)


class HolidayCalendarRepository(OrgScopedRepository[HolidayCalendar]):
    model = HolidayCalendar

    def list_calendars(self, *, status: str | None = None) -> Sequence[HolidayCalendar]:
        statement = self.select().order_by(HolidayCalendar.calendar_name, HolidayCalendar.id)
        if status is not None:
            statement = statement.where(HolidayCalendar.status == status)
        return list(self._session.execute(statement).scalars())

    def code_exists(self, code: str) -> bool:
        statement = (
            select(HolidayCalendar.id)
            .where(
                HolidayCalendar.organization_id == self._organization_id,
                HolidayCalendar.calendar_code_normalized == code.upper(),
            )
            .limit(1)
        )
        return self._session.execute(statement).first() is not None

    def active_default(self) -> HolidayCalendar | None:
        statement = self.select().where(HolidayCalendar.active_default_key == 1)
        return self._session.execute(statement).scalar_one_or_none()


class HolidayRepository(OrgScopedRepository[Holiday]):
    model = Holiday

    def list_for_calendar(
        self, calendar_id: uuid.UUID, *, start: date, end: date, include_cancelled: bool = False
    ) -> Sequence[Holiday]:
        """Holidays with ``start <= holiday_date <= end`` (both inclusive)."""
        statement = self.select().where(
            Holiday.calendar_id == calendar_id,
            Holiday.holiday_date >= start,
            Holiday.holiday_date <= end,
        )
        if not include_cancelled:
            statement = statement.where(Holiday.status == HolidayStatus.ACTIVE.value)
        return list(
            self._session.execute(statement.order_by(Holiday.holiday_date, Holiday.id)).scalars()
        )

    def active_between(self, calendar_id: uuid.UUID, start: date, end: date) -> Sequence[Holiday]:
        statement = self.select().where(
            Holiday.calendar_id == calendar_id,
            Holiday.holiday_date.between(start, end),
            Holiday.status == HolidayStatus.ACTIVE.value,
        )
        return list(self._session.execute(statement).scalars())

    def active_on(self, calendar_id: uuid.UUID, day: date) -> Holiday | None:
        statement = self.select().where(
            Holiday.calendar_id == calendar_id,
            Holiday.holiday_date == day,
            Holiday.status == HolidayStatus.ACTIVE.value,
        )
        return self._session.execute(statement).scalar_one_or_none()


class BranchCalendarAssignmentRepository(OrgScopedRepository[BranchHolidayCalendarAssignment]):
    model = BranchHolidayCalendarAssignment

    def for_branch(self, branch_id: uuid.UUID) -> Sequence[BranchHolidayCalendarAssignment]:
        statement = self.select().where(BranchHolidayCalendarAssignment.branch_id == branch_id)
        return list(
            self._session.execute(
                statement.order_by(BranchHolidayCalendarAssignment.effective_from)
            ).scalars()
        )

    def covering(self, branch_id: uuid.UUID, day: date) -> BranchHolidayCalendarAssignment | None:
        """The assignment whose ``[effective_from, effective_to)`` contains ``day``."""
        model = BranchHolidayCalendarAssignment
        statement = self.select().where(
            model.branch_id == branch_id,
            model.effective_from <= day,
            (model.effective_to.is_(None)) | (model.effective_to > day),
        )
        return self._session.execute(statement).scalar_one_or_none()


class EmployeeBranchResolver:
    """Branch of an employee as of a date, from ``employee_job_history`` (EMS §10.8)."""

    __slots__ = ("_organization_id", "_session")

    def __init__(self, session, organization_id: uuid.UUID) -> None:  # type: ignore[no-untyped-def]
        self._session = session
        self._organization_id = organization_id

    def branch_as_of(self, employee_id: uuid.UUID, day: date) -> uuid.UUID | None:
        statement = (
            select(EmployeeJobHistory.branch_id)
            .where(
                EmployeeJobHistory.organization_id == self._organization_id,
                EmployeeJobHistory.employee_id == employee_id,
                EmployeeJobHistory.effective_date <= day,
            )
            .order_by(
                EmployeeJobHistory.effective_date.desc(), EmployeeJobHistory.sequence_no.desc()
            )
            .limit(1)
        )
        found: uuid.UUID | None = self._session.execute(statement).scalar_one_or_none()
        return found


__all__ = [
    "BranchCalendarAssignmentRepository",
    "EmployeeBranchResolver",
    "HolidayCalendarRepository",
    "HolidayRepository",
    "WeeklyOffPatternRepository",
]
