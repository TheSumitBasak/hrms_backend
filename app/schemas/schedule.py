"""Request and response contracts for work schedules (addendum §5.2, §5.16)."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import Field, model_validator

from app.schemas.common import ApiModel

CODE_PATTERN = r"^[A-Z0-9][A-Z0-9_./-]{0,29}$"

OccurrenceValue = Literal["ALL", "FIRST", "SECOND", "THIRD", "FOURTH", "FIFTH", "LAST"]
StatusValue = Literal["ACTIVE", "INACTIVE"]
HolidayTypeValue = Literal["MANDATORY", "OPTIONAL"]


class WeeklyOffDay(ApiModel):
    day_of_week: Annotated[int, Field(ge=1, le=7)]
    occurrence: OccurrenceValue


class WeeklyOffPatternCreateRequest(ApiModel):
    pattern_code: Annotated[str, Field(pattern=CODE_PATTERN)]
    pattern_name: Annotated[str, Field(min_length=2, max_length=100)]
    days: list[WeeklyOffDay] = Field(default_factory=list, max_length=49)


class WeeklyOffPatternUpdateRequest(ApiModel):
    pattern_name: Annotated[str, Field(min_length=2, max_length=100)] | None = None
    status: StatusValue | None = None
    days: list[WeeklyOffDay] | None = Field(default=None, max_length=49)


class WeeklyOffPatternResponse(ApiModel):
    id: uuid.UUID
    organization_id: uuid.UUID
    pattern_code: str
    pattern_name: str
    status: str
    days: list[WeeklyOffDay]
    row_version: int


class HolidayCalendarCreateRequest(ApiModel):
    calendar_code: Annotated[str, Field(pattern=CODE_PATTERN)]
    calendar_name: Annotated[str, Field(min_length=2, max_length=150)]
    is_organization_default: bool = False


class HolidayCalendarUpdateRequest(ApiModel):
    calendar_name: Annotated[str, Field(min_length=2, max_length=150)] | None = None
    is_organization_default: bool | None = None
    status: StatusValue | None = None


class HolidayCalendarResponse(ApiModel):
    id: uuid.UUID
    organization_id: uuid.UUID
    calendar_code: str
    calendar_name: str
    is_organization_default: bool
    status: str
    row_version: int


class HolidayCreateRequest(ApiModel):
    holiday_date: date
    holiday_name: Annotated[str, Field(min_length=2, max_length=150)]
    holiday_type: HolidayTypeValue


class HolidayRenameRequest(ApiModel):
    holiday_name: Annotated[str, Field(min_length=2, max_length=150)]


class HolidayCancelRequest(ApiModel):
    cancellation_reason: Annotated[str, Field(min_length=10, max_length=500)]


class HolidayResponse(ApiModel):
    id: uuid.UUID
    organization_id: uuid.UUID
    calendar_id: uuid.UUID
    holiday_date: date
    holiday_name: str
    holiday_type: str
    status: str
    cancellation_reason: str | None
    row_version: int
    # Set when a past date is affected and attendance recalculation was queued (§7.6). The
    # recalculation job belongs to a later attendance stage, so this is null until then.
    recalculation_run_id: uuid.UUID | None = None


class CalendarAssignmentCreateRequest(ApiModel):
    calendar_id: uuid.UUID
    effective_from: date
    effective_to: date | None = None

    @model_validator(mode="after")
    def _range(self) -> CalendarAssignmentCreateRequest:
        if self.effective_to is not None and self.effective_to <= self.effective_from:
            raise ValueError("effective_to must be after effective_from")
        return self


class CalendarAssignmentResponse(ApiModel):
    id: uuid.UUID
    organization_id: uuid.UUID
    branch_id: uuid.UUID
    calendar_id: uuid.UUID
    effective_from: date
    effective_to: date | None
    row_version: int
    created_at: datetime


class ResolvedHoliday(ApiModel):
    holiday_id: uuid.UUID
    calendar_id: uuid.UUID
    holiday_date: date
    holiday_name: str
    holiday_type: str
