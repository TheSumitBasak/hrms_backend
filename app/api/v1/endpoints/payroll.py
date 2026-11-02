"""Payroll configuration: settings, pay groups, the period calendar and registration attributes
(payroll addendum §15.2)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Response, status

from app.api.deps import (
    CurrentOrg,
    DbSession,
    IfMatch,
    get_payroll_config_service,
    get_statutory_registration_service,
)
from app.core.masking import mask_identifier
from app.db.session import unit_of_work
from app.models.organization import OrganizationSettings
from app.models.payroll import (
    PayrollSettings,
    StatutoryRegistrationBranch,
    StatutoryRegistrationSetting,
)
from app.schemas.common import etag_for
from app.schemas.payroll import (
    PayGroupCreateRequest,
    PayGroupResponse,
    PayGroupUpdateRequest,
    PayrollPeriodResponse,
    PayrollPeriodUpdateRequest,
    PayrollSettingsRequest,
    PayrollSettingsResponse,
    StatutoryBranchInput,
    StatutoryRegistrationSettingRequest,
    StatutoryRegistrationSettingResponse,
)
from app.services.payroll_config import PayrollConfigService
from app.services.statutory_registration import StatutoryRegistrationService

router = APIRouter(prefix="/orgs/{org_id}", tags=["Payroll"])

Service = Annotated[PayrollConfigService, Depends(get_payroll_config_service)]
StatutoryService = Annotated[
    StatutoryRegistrationService, Depends(get_statutory_registration_service)
]


def _settings_response(
    row: PayrollSettings, flags: OrganizationSettings
) -> PayrollSettingsResponse:
    return PayrollSettingsResponse.model_validate(
        {
            **{
                field: getattr(row, field)
                for field in PayrollSettingsResponse.model_fields
                if hasattr(row, field)
            },
            "enable_payroll_module": flags.enable_payroll_module,
            "enable_payroll_self_service": flags.enable_payroll_self_service,
            "enable_payroll_alumni_access": flags.enable_payroll_alumni_access,
        }
    )


@router.get("/payroll-settings", response_model=PayrollSettingsResponse, summary="Read settings")
def read_settings(
    context: CurrentOrg, service: Service, response: Response
) -> PayrollSettingsResponse:
    row, flags = service.get_settings(context)
    response.headers["ETag"] = etag_for(row.row_version)
    return _settings_response(row, flags)


@router.put("/payroll-settings", response_model=PayrollSettingsResponse, summary="Replace settings")
def replace_settings(
    payload: PayrollSettingsRequest,
    context: CurrentOrg,
    if_match: IfMatch,
    db: DbSession,
    service: Service,
    response: Response,
) -> PayrollSettingsResponse:
    """Create the settings on the first call (no ``If-Match``), replace them afterwards."""
    with unit_of_work(db):
        row, flags = service.replace_settings(context, payload.model_dump(), if_match=if_match)
    result = _settings_response(row, flags)
    response.headers["ETag"] = etag_for(row.row_version)
    return result


@router.get("/pay-groups", response_model=list[PayGroupResponse], summary="List pay groups")
def list_groups(
    context: CurrentOrg, service: Service, include_inactive: bool = False
) -> list[PayGroupResponse]:
    return [
        PayGroupResponse.model_validate(row)
        for row in service.list_groups(context, include_inactive=include_inactive)
    ]


@router.post(
    "/pay-groups",
    response_model=PayGroupResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a pay group",
)
def create_group(
    payload: PayGroupCreateRequest,
    context: CurrentOrg,
    db: DbSession,
    service: Service,
    response: Response,
) -> PayGroupResponse:
    with unit_of_work(db):
        row = service.create_group(context, payload.model_dump())
    result = PayGroupResponse.model_validate(row)
    response.headers["ETag"] = etag_for(row.row_version)
    return result


@router.get("/pay-groups/{group_id}", response_model=PayGroupResponse, summary="Read a pay group")
def read_group(
    group_id: uuid.UUID, context: CurrentOrg, service: Service, response: Response
) -> PayGroupResponse:
    row = service.get_group(context, group_id)
    response.headers["ETag"] = etag_for(row.row_version)
    return PayGroupResponse.model_validate(row)


@router.patch("/pay-groups/{group_id}", response_model=PayGroupResponse, summary="Edit a pay group")
def update_group(
    group_id: uuid.UUID,
    payload: PayGroupUpdateRequest,
    context: CurrentOrg,
    if_match: IfMatch,
    db: DbSession,
    service: Service,
    response: Response,
) -> PayGroupResponse:
    """Cycle changes apply to periods generated afterwards; existing periods are kept."""
    with unit_of_work(db):
        row = service.update_group(
            context,
            group_id,
            payload.model_dump(exclude_unset=True),
            if_match=if_match,
        )
    result = PayGroupResponse.model_validate(row)
    response.headers["ETag"] = etag_for(row.row_version)
    return result


@router.get(
    "/payroll-periods", response_model=list[PayrollPeriodResponse], summary="List payroll periods"
)
def list_periods(
    context: CurrentOrg,
    service: Service,
    pay_group_id: uuid.UUID | None = None,
    start: date | None = None,
    end: date | None = None,
) -> list[PayrollPeriodResponse]:
    return [
        PayrollPeriodResponse.model_validate(row)
        for row in service.list_periods(context, pay_group_id=pay_group_id, start=start, end=end)
    ]


@router.patch(
    "/payroll-periods/{period_id}",
    response_model=PayrollPeriodResponse,
    summary="Edit a period's target payment date",
)
def update_period(
    period_id: uuid.UUID,
    payload: PayrollPeriodUpdateRequest,
    context: CurrentOrg,
    if_match: IfMatch,
    db: DbSession,
    service: Service,
    response: Response,
) -> PayrollPeriodResponse:
    with unit_of_work(db):
        row = service.update_period(
            context,
            period_id,
            target_payment_date=payload.target_payment_date,
            acknowledged_warnings=payload.acknowledged_warnings,
            if_match=if_match,
        )
    result = PayrollPeriodResponse.model_validate(row)
    response.headers["ETag"] = etag_for(row.row_version)
    return result


def _registration_setting_response(
    row: StatutoryRegistrationSetting, branches: Sequence[StatutoryRegistrationBranch]
) -> StatutoryRegistrationSettingResponse:
    last_four = row.responsible_person_pan_last_four
    return StatutoryRegistrationSettingResponse(
        id=row.id,
        registration_id=row.registration_id,
        pf_contribution_rate_category=row.pf_contribution_rate_category,
        pf_exempted_trust=row.pf_exempted_trust,
        pf_trust_name=row.pf_trust_name,
        esi_implemented_area=row.esi_implemented_area,
        deductor_category=row.deductor_category,
        responsible_person_name=row.responsible_person_name,
        responsible_person_designation=row.responsible_person_designation,
        responsible_person_pan_masked=mask_identifier("PAN", last_four) if last_four else None,
        branches=[StatutoryBranchInput.model_validate(branch) for branch in branches],
        row_version=row.row_version,
    )


@router.get(
    "/statutory-registration-settings/{registration_id}",
    response_model=StatutoryRegistrationSettingResponse,
    summary="Read a registration's payroll attributes",
)
def read_registration_settings(
    registration_id: uuid.UUID, context: CurrentOrg, service: StatutoryService, response: Response
) -> StatutoryRegistrationSettingResponse:
    row, branches = service.get(context, registration_id)
    response.headers["ETag"] = etag_for(row.row_version)
    return _registration_setting_response(row, branches)


@router.put(
    "/statutory-registration-settings/{registration_id}",
    response_model=StatutoryRegistrationSettingResponse,
    summary="Create or replace a registration's payroll attributes",
)
def replace_registration_settings(
    registration_id: uuid.UUID,
    payload: StatutoryRegistrationSettingRequest,
    context: CurrentOrg,
    if_match: IfMatch,
    db: DbSession,
    service: StatutoryService,
    response: Response,
) -> StatutoryRegistrationSettingResponse:
    """Create on the first call (no ``If-Match``), replace afterwards."""
    with unit_of_work(db):
        row, branches = service.replace(
            context, registration_id, payload.model_dump(), if_match=if_match
        )
    result = _registration_setting_response(row, branches)
    response.headers["ETag"] = etag_for(row.row_version)
    return result
