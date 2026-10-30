"""Employee status as of a date (ATT-02), shared by attendance eligibility and leave accrual."""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.employee import Employee, EmployeeStatusHistory


def status_on(
    session: Session, organization_id: uuid.UUID, employee: Employee, day: date, *, today: date
) -> str | None:
    """The ``to_status`` of the latest history row effective on ``day`` (ties by ``created_at``).

    For today the current ``employees.status`` is authoritative; with no history row the current
    status is the fallback.
    """
    if day >= today:
        return employee.status
    statement = (
        select(EmployeeStatusHistory.to_status)
        .where(
            EmployeeStatusHistory.organization_id == organization_id,
            EmployeeStatusHistory.employee_id == employee.id,
            EmployeeStatusHistory.status_effective_date <= day,
        )
        .order_by(
            EmployeeStatusHistory.status_effective_date.desc(),
            EmployeeStatusHistory.created_at.desc(),
        )
        .limit(1)
    )
    found = session.execute(statement).scalar_one_or_none()
    return found if found is not None else employee.status


ATTENDANCE_STATUSES = frozenset({"ACTIVE", "ON_NOTICE"})


def attendance_eligible(
    session: Session,
    organization_id: uuid.UUID,
    tracking_start: date | None,
    employee: Employee,
    day: date,
    *,
    today: date,
) -> bool:
    """ATT-01 (b)-(e): tracking started, not archived, within employment dates, status in force."""
    if (
        (tracking_start is not None and day < tracking_start)
        or employee.archived_at is not None
        or employee.joining_date is None
        or day < employee.joining_date
        or (employee.employment_end_date is not None and day > employee.employment_end_date)
    ):
        return False
    return status_on(session, organization_id, employee, day, today=today) in ATTENDANCE_STATUSES
