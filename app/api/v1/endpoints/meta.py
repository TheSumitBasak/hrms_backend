"""Discovery endpoints: API capabilities and the caller's organization context.

Handlers stay thin: they read through repositories and shape a response. There is no business
rule here, only a projection of settings the caller is already entitled to see.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from app import __version__
from app.api.deps import (
    CurrentOrg,
    get_app_settings,
    get_employee_repository,
    get_organization_repository,
    get_organization_settings_repository,
)
from app.core.config import Settings
from app.core.errors import ResourceNotFound
from app.repositories.employee import EmployeeRepository
from app.repositories.organization import (
    OrganizationRepository,
    OrganizationSettingsRepository,
)
from app.schemas.meta import (
    ContextAttendance,
    ContextDocuments,
    ContextLeave,
    ContextMe,
    ContextModules,
    ContextOrganization,
    ContextSelfService,
    MetaResponse,
    OrganizationContextResponse,
)

router = APIRouter(tags=["Meta"])

# Registered by hand on purpose: a capability string is a promise to the frontend, so it is
# added in the same change that ships the feature, never inferred from the route table.
CAPABILITIES: tuple[str, ...] = ("auth.session_policy", "meta", "org.me_context")


@router.get("/meta", response_model=MetaResponse, summary="API version and capabilities")
def read_meta(settings: Annotated[Settings, Depends(get_app_settings)]) -> MetaResponse:
    """Unauthenticated by design: it holds no tenant data and the frontend reads it pre-login."""
    return MetaResponse(
        api_version=__version__,
        build_sha=settings.build_sha,
        capabilities=sorted(CAPABILITIES),
    )


@router.get(
    "/orgs/{org_id}/me/context",
    response_model=OrganizationContextResponse,
    summary="Modules, dates and self-service flags for the caller",
)
def read_organization_context(
    context: CurrentOrg,
    organizations: Annotated[OrganizationRepository, Depends(get_organization_repository)],
    settings_repo: Annotated[
        OrganizationSettingsRepository, Depends(get_organization_settings_repository)
    ],
    employees: Annotated[EmployeeRepository, Depends(get_employee_repository)],
) -> OrganizationContextResponse:
    """Readable by any user holding a grant in the organization, including SELF."""
    organization = organizations.get(context.organization_id)
    if organization is None or organization.archived_at is not None:
        raise ResourceNotFound("organization")
    cfg = settings_repo.require(context.organization_id)

    linked = employees.get(context.employee_id) if context.employee_id else None
    return OrganizationContextResponse(
        organization=ContextOrganization(
            id=organization.id,
            code=organization.organization_code,
            display_name=organization.display_name,
            status=organization.status,
            timezone=organization.timezone,
            default_currency=organization.default_currency,
            financial_year_start_month=organization.financial_year_start_month,
            date_display_format=cfg.date_display_format,
        ),
        modules=ContextModules(
            onboarding=cfg.enable_onboarding_module,
            attendance=cfg.enable_attendance_module,
            leave=cfg.enable_leave_module,
        ),
        self_service=ContextSelfService(
            onboarding=cfg.enable_onboarding_self_service,
            attendance=cfg.enable_attendance_self_service,
            leave=cfg.enable_leave_self_service,
            employee_self_view=cfg.enable_employee_self_view,
            self_view_salary=cfg.self_view_salary,
        ),
        leave=ContextLeave(
            leave_year_start_month=cfg.leave_year_start_month,
            leave_tracking_start_date=cfg.leave_tracking_start_date,
        ),
        attendance=ContextAttendance(
            attendance_tracking_start_date=cfg.attendance_tracking_start_date,
            day_close_time=cfg.attendance_day_close_time.strftime("%H:%M"),
        ),
        documents=ContextDocuments(expiry_warning_days=cfg.document_expiry_warning_days),
        me=ContextMe(
            employee_id=linked.id if linked else None,
            employee_status=linked.status if linked else None,
        ),
    )
