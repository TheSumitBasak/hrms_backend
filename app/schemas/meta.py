"""Contracts for the discovery endpoints: ``/meta`` (BE-S09) and ``me/context`` (BE-S01)."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Literal

from app.schemas.common import ApiModel


class MetaResponse(ApiModel):
    """``GET /api/v1/meta`` — lets a client switch features on without a coordinated release."""

    api_version: str
    build_sha: str
    capabilities: list[str]


class ContextOrganization(ApiModel):
    id: uuid.UUID
    code: str
    display_name: str
    status: str
    timezone: str
    default_currency: str
    financial_year_start_month: int
    date_display_format: Literal["DD-MM-YYYY", "YYYY-MM-DD"]


class ContextModules(ApiModel):
    onboarding: bool
    attendance: bool
    leave: bool
    payroll: bool = False


class ContextSelfService(ApiModel):
    onboarding: bool
    attendance: bool
    leave: bool
    payroll: bool = False
    employee_self_view: bool
    self_view_salary: bool


class ContextLeave(ApiModel):
    leave_year_start_month: int
    leave_tracking_start_date: date | None


class ContextAttendance(ApiModel):
    attendance_tracking_start_date: date | None
    day_close_time: str
    """``HH:MM`` in the organization's time zone."""


class ContextDocuments(ApiModel):
    expiry_warning_days: int


class ContextMe(ApiModel):
    employee_id: uuid.UUID | None
    employee_status: str | None


class OrganizationContextResponse(ApiModel):
    """``GET /orgs/{org_id}/me/context`` — non-sensitive settings any granted user may read.

    Carries no admin-only settings (those stay behind ``org.manage``), so SELF users can learn
    which modules are on without being able to read the full settings document.
    """

    organization: ContextOrganization
    modules: ContextModules
    self_service: ContextSelfService
    leave: ContextLeave
    attendance: ContextAttendance
    documents: ContextDocuments
    me: ContextMe
