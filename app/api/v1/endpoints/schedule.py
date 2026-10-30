"""Weekly-off patterns, holiday calendars, holidays and branch calendar assignments (§5.2, §5.16).

Handlers parse, delegate and serialise; every rule lives in
:class:`~app.services.schedule.ScheduleService`.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Response, status

from app.api.deps import CurrentOrg, DbSession, IfMatch, get_schedule_service
from app.core.errors import ValidationFailed
from app.db.session import unit_of_work
from app.models.attendance import (
    BranchHolidayCalendarAssignment,
    Holiday,
    HolidayCalendar,
    WeeklyOffPattern,
    WeeklyOffPatternDay,
)
from app.schemas.common import Page, VersionedPayload, etag_for
from app.schemas.schedule import (
    CalendarAssignmentCreateRequest,
    CalendarAssignmentResponse,
    HolidayCalendarCreateRequest,
    HolidayCalendarResponse,
    HolidayCalendarUpdateRequest,
    HolidayCancelRequest,
    HolidayCreateRequest,
    HolidayRenameRequest,
    HolidayResponse,
    ResolvedHoliday,
    WeeklyOffDay,
    WeeklyOffPatternCreateRequest,
    WeeklyOffPatternResponse,
    WeeklyOffPatternUpdateRequest,
)
from app.services.schedule import ScheduleService

router = APIRouter(prefix="/orgs/{org_id}", tags=["Work schedules"])

Service = Annotated[ScheduleService, Depends(get_schedule_service)]


def _finalise[ModelT: VersionedPayload](response: Response, row: Any, result: ModelT) -> ModelT:
    """Publish the committed ``row_version`` as ETag and body field (see job_catalog)."""
    response.headers["ETag"] = etag_for(row.row_version)
    result.row_version = row.row_version
    return result


def _pattern(
    pattern: WeeklyOffPattern, days: list[WeeklyOffPatternDay]
) -> WeeklyOffPatternResponse:
    return WeeklyOffPatternResponse(
        id=pattern.id,
        organization_id=pattern.organization_id,
        pattern_code=pattern.pattern_code,
        pattern_name=pattern.pattern_name,
        status=pattern.status,
        days=[WeeklyOffDay(day_of_week=d.day_of_week, occurrence=d.occurrence) for d in days],
        row_version=pattern.row_version,
    )


def _calendar(row: HolidayCalendar) -> HolidayCalendarResponse:
    return HolidayCalendarResponse.model_validate(row)


def _holiday(row: Holiday) -> HolidayResponse:
    return HolidayResponse.model_validate(row)


def _assignment(row: BranchHolidayCalendarAssignment) -> CalendarAssignmentResponse:
    return CalendarAssignmentResponse.model_validate(row)


# ------------------------------------------------------------- weekly-off patterns


@router.get("/weekly-off-patterns", response_model=Page[WeeklyOffPatternResponse])
def list_weekly_off_patterns(
    context: CurrentOrg,
    service: Service,
    status_filter: Annotated[
        str | None, Query(alias="status", pattern="^(ACTIVE|INACTIVE)$")
    ] = None,
) -> Page[WeeklyOffPatternResponse]:
    rows, days = service.list_patterns(context, status=status_filter)
    return Page[WeeklyOffPatternResponse](items=[_pattern(r, days[r.id]) for r in rows])


@router.post(
    "/weekly-off-patterns",
    response_model=WeeklyOffPatternResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_weekly_off_pattern(
    payload: WeeklyOffPatternCreateRequest,
    response: Response,
    context: CurrentOrg,
    db: DbSession,
    service: Service,
) -> WeeklyOffPatternResponse:
    values = payload.model_dump()
    with unit_of_work(db):
        pattern, days = service.create_pattern(context, values=values)
        result = _pattern(pattern, days)
    return _finalise(response, pattern, result)


@router.get("/weekly-off-patterns/{pattern_id}", response_model=WeeklyOffPatternResponse)
def read_weekly_off_pattern(
    pattern_id: uuid.UUID, response: Response, context: CurrentOrg, service: Service
) -> WeeklyOffPatternResponse:
    pattern, days = service.get_pattern(context, pattern_id)
    response.headers["ETag"] = etag_for(pattern.row_version)
    return _pattern(pattern, days)


@router.patch("/weekly-off-patterns/{pattern_id}", response_model=WeeklyOffPatternResponse)
def update_weekly_off_pattern(
    pattern_id: uuid.UUID,
    payload: WeeklyOffPatternUpdateRequest,
    response: Response,
    context: CurrentOrg,
    if_match: IfMatch,
    db: DbSession,
    service: Service,
) -> WeeklyOffPatternResponse:
    changes = payload.model_dump(exclude_unset=True)
    with unit_of_work(db):
        pattern, days = service.update_pattern(
            context, pattern_id=pattern_id, changes=changes, if_match=if_match
        )
        result = _pattern(pattern, days)
    return _finalise(response, pattern, result)


# --------------------------------------------------------------- holiday calendars


@router.get("/holiday-calendars", response_model=Page[HolidayCalendarResponse])
def list_holiday_calendars(
    context: CurrentOrg,
    service: Service,
    status_filter: Annotated[
        str | None, Query(alias="status", pattern="^(ACTIVE|INACTIVE)$")
    ] = None,
) -> Page[HolidayCalendarResponse]:
    return Page[HolidayCalendarResponse](
        items=[_calendar(r) for r in service.list_calendars(context, status=status_filter)]
    )


@router.post(
    "/holiday-calendars",
    response_model=HolidayCalendarResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_holiday_calendar(
    payload: HolidayCalendarCreateRequest,
    response: Response,
    context: CurrentOrg,
    db: DbSession,
    service: Service,
) -> HolidayCalendarResponse:
    with unit_of_work(db):
        row = service.create_calendar(context, values=payload.model_dump())
        result = _calendar(row)
    return _finalise(response, row, result)


@router.get("/holiday-calendars/{calendar_id}", response_model=HolidayCalendarResponse)
def read_holiday_calendar(
    calendar_id: uuid.UUID, response: Response, context: CurrentOrg, service: Service
) -> HolidayCalendarResponse:
    row = service.get_calendar(context, calendar_id)
    response.headers["ETag"] = etag_for(row.row_version)
    return _calendar(row)


@router.patch("/holiday-calendars/{calendar_id}", response_model=HolidayCalendarResponse)
def update_holiday_calendar(
    calendar_id: uuid.UUID,
    payload: HolidayCalendarUpdateRequest,
    response: Response,
    context: CurrentOrg,
    if_match: IfMatch,
    db: DbSession,
    service: Service,
) -> HolidayCalendarResponse:
    with unit_of_work(db):
        row = service.update_calendar(
            context,
            calendar_id=calendar_id,
            changes=payload.model_dump(exclude_unset=True),
            if_match=if_match,
        )
        result = _calendar(row)
    return _finalise(response, row, result)


# ------------------------------------------------------------------------- holidays


@router.get("/holiday-calendars/{calendar_id}/holidays", response_model=Page[HolidayResponse])
def list_holidays(
    calendar_id: uuid.UUID,
    context: CurrentOrg,
    service: Service,
    year: Annotated[int | None, Query(ge=2000, le=2200)] = None,
    date_from: Annotated[date | None, Query(alias="from")] = None,
    date_to: Annotated[date | None, Query(alias="to")] = None,
    include_cancelled: bool = False,
) -> Page[HolidayResponse]:
    if year is not None and (date_from or date_to):
        raise ValidationFailed("Supply either year or from/to, not both.")
    if (date_from is None) != (date_to is None):
        raise ValidationFailed("from and to must be supplied together.")
    rows = service.list_holidays(
        context,
        calendar_id=calendar_id,
        year=year,
        date_range=(date_from, date_to) if date_from and date_to else None,
        include_cancelled=include_cancelled,
    )
    return Page[HolidayResponse](items=[_holiday(r) for r in rows])


@router.post(
    "/holiday-calendars/{calendar_id}/holidays",
    response_model=HolidayResponse,
    status_code=status.HTTP_201_CREATED,
)
def add_holiday(
    calendar_id: uuid.UUID,
    payload: HolidayCreateRequest,
    response: Response,
    context: CurrentOrg,
    db: DbSession,
    service: Service,
) -> HolidayResponse:
    with unit_of_work(db):
        row = service.add_holiday(context, calendar_id=calendar_id, values=payload.model_dump())
        result = _holiday(row)
    return _finalise(response, row, result)


@router.patch(
    "/holiday-calendars/{calendar_id}/holidays/{holiday_id}", response_model=HolidayResponse
)
def rename_holiday(
    calendar_id: uuid.UUID,
    holiday_id: uuid.UUID,
    payload: HolidayRenameRequest,
    response: Response,
    context: CurrentOrg,
    if_match: IfMatch,
    db: DbSession,
    service: Service,
) -> HolidayResponse:
    with unit_of_work(db):
        row = service.rename_holiday(
            context, holiday_id=holiday_id, name=payload.holiday_name, if_match=if_match
        )
        result = _holiday(row)
    return _finalise(response, row, result)


@router.post(
    "/holiday-calendars/{calendar_id}/holidays/{holiday_id}/cancel", response_model=HolidayResponse
)
def cancel_holiday(
    calendar_id: uuid.UUID,
    holiday_id: uuid.UUID,
    payload: HolidayCancelRequest,
    response: Response,
    context: CurrentOrg,
    if_match: IfMatch,
    db: DbSession,
    service: Service,
) -> HolidayResponse:
    with unit_of_work(db):
        row = service.cancel_holiday(
            context, holiday_id=holiday_id, reason=payload.cancellation_reason, if_match=if_match
        )
        result = _holiday(row)
    return _finalise(response, row, result)


# ------------------------------------------------------ branch calendar assignments


@router.get(
    "/branches/{branch_id}/holiday-calendar-assignments",
    response_model=Page[CalendarAssignmentResponse],
)
def list_calendar_assignments(
    branch_id: uuid.UUID, context: CurrentOrg, service: Service
) -> Page[CalendarAssignmentResponse]:
    rows = service.list_assignments(context, branch_id=branch_id)
    return Page[CalendarAssignmentResponse](items=[_assignment(r) for r in rows])


@router.post(
    "/branches/{branch_id}/holiday-calendar-assignments",
    response_model=CalendarAssignmentResponse,
    status_code=status.HTTP_201_CREATED,
)
def assign_calendar(
    branch_id: uuid.UUID,
    payload: CalendarAssignmentCreateRequest,
    response: Response,
    context: CurrentOrg,
    db: DbSession,
    service: Service,
) -> CalendarAssignmentResponse:
    with unit_of_work(db):
        row = service.assign_calendar(context, branch_id=branch_id, values=payload.model_dump())
        result = _assignment(row)
    return _finalise(response, row, result)


@router.get("/employees/{employee_id}/holidays", response_model=Page[ResolvedHoliday])
def employee_holidays(
    employee_id: uuid.UUID,
    context: CurrentOrg,
    service: Service,
    date_from: Annotated[date, Query(alias="from")],
    date_to: Annotated[date, Query(alias="to")],
) -> Page[ResolvedHoliday]:
    rows = service.holidays_for_employee(
        context, employee_id=employee_id, start=date_from, end=date_to
    )
    return Page[ResolvedHoliday](
        items=[
            ResolvedHoliday(
                holiday_id=r.id,
                calendar_id=r.calendar_id,
                holiday_date=r.holiday_date,
                holiday_name=r.holiday_name,
                holiday_type=r.holiday_type,
            )
            for r in rows
        ]
    )
