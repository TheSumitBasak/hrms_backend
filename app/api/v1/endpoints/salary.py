"""Salary components, templates, assignments and timeline operations (§24.4.6)."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Response, status

from app.api.deps import CurrentOrg, DbSession, IfMatch, get_salary_service
from app.db.session import unit_of_work
from app.models.salary import (
    EmployeeSalaryAssignment,
    EmployeeSalaryComponent,
    SalaryTemplateVersion,
)
from app.schemas.common import ReasonRequest, etag_for
from app.schemas.salary import (
    ApproveRequest,
    ComponentUsageResponse,
    ConfirmRequest,
    CorrectRequest,
    PreviewResponse,
    RejectRequest,
    ReplaceRequest,
    SalaryAssignmentCreateRequest,
    SalaryAssignmentResponse,
    SalaryAssignmentUpdateRequest,
    SalaryCompareResponse,
    SalaryComponentCreateRequest,
    SalaryComponentResponse,
    SalaryComponentUpdateRequest,
    SalaryHistoryResponse,
    SalaryTemplateCreateRequest,
    SalaryTemplateResponse,
    SalaryTemplateUpdateRequest,
    TemplateMappingsReplaceRequest,
    TemplatePublishRequest,
    TemplateVersionResponse,
    TemplateVersionUpdateRequest,
)
from app.services.salary import SalaryService

router = APIRouter(prefix="/orgs/{org_id}", tags=["Salary"])


def _component_payload(row: EmployeeSalaryComponent) -> dict[str, Any]:
    return {
        "component_id": row.component_id,
        "component_code": row.component_code_snapshot,
        "component_name": row.component_name_snapshot,
        "display_name": row.display_name_snapshot,
        "component_type": row.component_type_snapshot,
        "component_category": row.component_category_snapshot,
        "value_type": row.value_type,
        "amount_period": row.amount_period_snapshot,
        "value_amount": None if row.value_amount is None else str(row.value_amount),
        "value_percentage": None if row.value_percentage is None else str(row.value_percentage),
        "value_text": row.value_text,
        "value_boolean": row.value_boolean,
        "computed_amount": None if row.computed_amount is None else str(row.computed_amount),
        "template_default_amount": None
        if row.template_default_amount is None
        else str(row.template_default_amount),
        "template_default_percentage": None
        if row.template_default_percentage is None
        else str(row.template_default_percentage),
        "template_default_text": row.template_default_text,
        "template_default_boolean": row.template_default_boolean,
        "is_overridden": row.is_overridden,
        "override_reason": row.override_reason,
        "is_custom": row.is_custom,
    }


def _assignment_payload(
    service: SalaryService, row: EmployeeSalaryAssignment
) -> SalaryAssignmentResponse:
    return SalaryAssignmentResponse.model_validate(
        {
            **{
                field: getattr(row, field)
                for field in SalaryAssignmentResponse.model_fields
                if hasattr(row, field) and field != "components"
            },
            "components": [_component_payload(item) for item in service.components_for(row.id)],
        }
    )


def _version_payload(
    service: SalaryService, version: SalaryTemplateVersion
) -> TemplateVersionResponse:
    return TemplateVersionResponse(
        id=version.id,
        template_id=version.template_id,
        version_number=version.version_number,
        currency=version.currency,
        status=version.status,
        effective_from=version.effective_from,
        effective_to=version.effective_to,
        change_note=version.change_note,
        mappings=[
            {
                "component_id": row.component_id,
                "default_value_amount": row.default_value_amount,
                "default_value_percentage": row.default_value_percentage,
                "default_value_text": row.default_value_text,
                "default_value_boolean": row.default_value_boolean,
                "is_required": row.is_required,
                "is_overridable": row.is_overridable,
                "display_order": row.display_order,
            }
            for row in service._mappings.list_for_version(version.id)
        ],
        row_version=version.row_version,
    )


# --------------------------------------------------------------------------- components


@router.get(
    "/salary-components",
    response_model=list[SalaryComponentResponse],
    summary="List salary components",
)
def list_components(
    context: CurrentOrg,
    service: Annotated[SalaryService, Depends(get_salary_service)],
    include_inactive: bool = False,
) -> list[SalaryComponentResponse]:
    return [
        SalaryComponentResponse.model_validate(row)
        for row in service.list_components(context, include_inactive=include_inactive)
    ]


@router.post(
    "/salary-components",
    response_model=SalaryComponentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a salary component",
)
def create_component(
    payload: SalaryComponentCreateRequest,
    context: CurrentOrg,
    db: DbSession,
    service: Annotated[SalaryService, Depends(get_salary_service)],
) -> SalaryComponentResponse:
    with unit_of_work(db):
        row = service.create_component(context, payload.model_dump())
    return SalaryComponentResponse.model_validate(row)


@router.patch(
    "/salary-components/{component_id}",
    response_model=SalaryComponentResponse,
    summary="Edit a salary component",
)
def update_component(
    component_id: uuid.UUID,
    payload: SalaryComponentUpdateRequest,
    response: Response,
    context: CurrentOrg,
    if_match: IfMatch,
    db: DbSession,
    service: Annotated[SalaryService, Depends(get_salary_service)],
) -> SalaryComponentResponse:
    with unit_of_work(db):
        row = service.update_component(
            context,
            component_id,
            payload.model_dump(exclude_unset=True),
            if_match=if_match,
        )
        result = SalaryComponentResponse.model_validate(row)
    response.headers["ETag"] = etag_for(row.row_version)
    return result


@router.get(
    "/salary-components/{component_id}/usage",
    response_model=ComponentUsageResponse,
    summary="Count the template versions and assignments using a component",
)
def component_usage(
    component_id: uuid.UUID,
    context: CurrentOrg,
    service: Annotated[SalaryService, Depends(get_salary_service)],
) -> ComponentUsageResponse:
    return ComponentUsageResponse(**service.component_usage(context, component_id))


@router.post(
    "/salary-components/{component_id}/deactivate",
    response_model=SalaryComponentResponse,
    summary="Deactivate a salary component",
)
def deactivate_component(
    component_id: uuid.UUID,
    payload: ReasonRequest,
    context: CurrentOrg,
    db: DbSession,
    service: Annotated[SalaryService, Depends(get_salary_service)],
) -> SalaryComponentResponse:
    with unit_of_work(db):
        row = service.deactivate_component(context, component_id, payload.reason)
    return SalaryComponentResponse.model_validate(row)


# --------------------------------------------------------------------------- templates


@router.get(
    "/salary-templates",
    response_model=list[SalaryTemplateResponse],
    summary="List salary templates",
)
def list_templates(
    context: CurrentOrg,
    service: Annotated[SalaryService, Depends(get_salary_service)],
    include_archived: bool = False,
) -> list[SalaryTemplateResponse]:
    return [
        SalaryTemplateResponse.model_validate(row)
        for row in service.list_templates(context, include_archived=include_archived)
    ]


@router.post(
    "/salary-templates",
    response_model=TemplateVersionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a salary template with draft v1",
)
def create_template(
    payload: SalaryTemplateCreateRequest,
    context: CurrentOrg,
    db: DbSession,
    service: Annotated[SalaryService, Depends(get_salary_service)],
) -> TemplateVersionResponse:
    values = payload.model_dump()
    values["mappings"] = payload.mappings
    with unit_of_work(db):
        _, version = service.create_template(context, values)
        return _version_payload(service, version)


@router.get(
    "/salary-templates/{template_id}",
    response_model=SalaryTemplateResponse,
    summary="Read a salary template",
)
def read_template(
    template_id: uuid.UUID,
    response: Response,
    context: CurrentOrg,
    service: Annotated[SalaryService, Depends(get_salary_service)],
) -> SalaryTemplateResponse:
    row = service._templates.require(template_id, resource="salary template")
    response.headers["ETag"] = etag_for(row.row_version)
    return SalaryTemplateResponse.model_validate(row)


@router.patch(
    "/salary-templates/{template_id}",
    response_model=SalaryTemplateResponse,
    summary="Edit a salary template header",
)
def update_template(
    template_id: uuid.UUID,
    payload: SalaryTemplateUpdateRequest,
    response: Response,
    context: CurrentOrg,
    if_match: IfMatch,
    db: DbSession,
    service: Annotated[SalaryService, Depends(get_salary_service)],
) -> SalaryTemplateResponse:
    with unit_of_work(db):
        row = service.update_template(
            context,
            template_id,
            payload.model_dump(exclude_unset=True),
            if_match=if_match,
        )
        result = SalaryTemplateResponse.model_validate(row)
    response.headers["ETag"] = etag_for(row.row_version)
    return result


@router.get(
    "/salary-templates/{template_id}/versions",
    response_model=list[TemplateVersionResponse],
    summary="List every version of a template (mappings omitted)",
)
def list_versions(
    template_id: uuid.UUID,
    context: CurrentOrg,
    service: Annotated[SalaryService, Depends(get_salary_service)],
) -> list[TemplateVersionResponse]:
    return [
        TemplateVersionResponse.model_validate(row)
        for row in service.list_versions(context, template_id)
    ]


@router.get(
    "/salary-templates/{template_id}/versions/{version_id}",
    response_model=TemplateVersionResponse,
    summary="Read one template version with its mappings",
)
def read_version(
    template_id: uuid.UUID,
    version_id: uuid.UUID,
    response: Response,
    context: CurrentOrg,
    service: Annotated[SalaryService, Depends(get_salary_service)],
) -> TemplateVersionResponse:
    version = service.get_version(context, template_id, version_id)
    response.headers["ETag"] = etag_for(version.row_version)
    return _version_payload(service, version)


@router.post(
    "/salary-templates/{template_id}/versions",
    response_model=TemplateVersionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new draft version",
)
def new_version(
    template_id: uuid.UUID,
    context: CurrentOrg,
    db: DbSession,
    service: Annotated[SalaryService, Depends(get_salary_service)],
) -> TemplateVersionResponse:
    with unit_of_work(db):
        version = service.new_version(context, template_id)
        return _version_payload(service, version)


@router.patch(
    "/salary-templates/{template_id}/versions/{version_id}",
    response_model=TemplateVersionResponse,
    summary="Edit a draft version",
)
def update_version(
    template_id: uuid.UUID,
    version_id: uuid.UUID,
    payload: TemplateVersionUpdateRequest,
    response: Response,
    context: CurrentOrg,
    if_match: IfMatch,
    db: DbSession,
    service: Annotated[SalaryService, Depends(get_salary_service)],
) -> TemplateVersionResponse:
    with unit_of_work(db):
        version = service.update_version(
            context,
            template_id,
            version_id,
            payload.model_dump(exclude_unset=True),
            if_match=if_match,
        )
        result = _version_payload(service, version)
    response.headers["ETag"] = etag_for(version.row_version)
    return result


@router.put(
    "/salary-templates/{template_id}/versions/{version_id}/components",
    response_model=TemplateVersionResponse,
    summary="Replace draft component mappings",
)
def replace_mappings(
    template_id: uuid.UUID,
    version_id: uuid.UUID,
    payload: TemplateMappingsReplaceRequest,
    context: CurrentOrg,
    db: DbSession,
    service: Annotated[SalaryService, Depends(get_salary_service)],
) -> TemplateVersionResponse:
    with unit_of_work(db):
        version = service.replace_mappings(context, template_id, version_id, payload.mappings)
        return _version_payload(service, version)


@router.post(
    "/salary-templates/{template_id}/versions/{version_id}/publish",
    response_model=TemplateVersionResponse,
    summary="Publish a template version",
)
def publish_version(
    template_id: uuid.UUID,
    version_id: uuid.UUID,
    context: CurrentOrg,
    db: DbSession,
    service: Annotated[SalaryService, Depends(get_salary_service)],
    payload: TemplatePublishRequest | None = None,
) -> TemplateVersionResponse:
    changes = payload.model_dump(exclude_unset=True, exclude={"mappings"}) if payload else None
    with unit_of_work(db):
        version = service.publish_version(
            context,
            template_id,
            version_id,
            changes=changes,
            mappings=payload.mappings if payload else None,
        )
        return _version_payload(service, version)


@router.post(
    "/salary-templates/{template_id}/archive",
    response_model=SalaryTemplateResponse,
    summary="Archive a salary template",
)
def archive_template(
    template_id: uuid.UUID,
    payload: ReasonRequest,
    context: CurrentOrg,
    db: DbSession,
    service: Annotated[SalaryService, Depends(get_salary_service)],
) -> SalaryTemplateResponse:
    with unit_of_work(db):
        row = service.archive_template(context, template_id, payload.reason)
    return SalaryTemplateResponse.model_validate(row)


# --------------------------------------------------------------------------- assignments


@router.get(
    "/employees/{employee_id}/salary-assignments",
    response_model=list[SalaryAssignmentResponse],
    summary="Read the salary timeline",
)
def timeline(
    employee_id: uuid.UUID,
    context: CurrentOrg,
    service: Annotated[SalaryService, Depends(get_salary_service)],
    as_of: date | None = None,
    include_cancelled: bool = False,
) -> list[SalaryAssignmentResponse]:
    return [
        _assignment_payload(service, row)
        for row in service.timeline(
            context, employee_id, as_of=as_of, include_cancelled=include_cancelled
        )
    ]


@router.get(
    "/employees/{employee_id}/salary-assignments/current",
    response_model=SalaryAssignmentResponse | None,
    summary="Read the current salary as of today",
)
def current_salary(
    employee_id: uuid.UUID,
    context: CurrentOrg,
    service: Annotated[SalaryService, Depends(get_salary_service)],
) -> SalaryAssignmentResponse | None:
    row = service.current(context, employee_id)
    return _assignment_payload(service, row) if row else None


@router.get(
    "/employees/{employee_id}/salary-assignments/compare",
    response_model=SalaryCompareResponse,
    summary="Compare two assignments without arithmetic",
)
def compare(
    employee_id: uuid.UUID,
    context: CurrentOrg,
    service: Annotated[SalaryService, Depends(get_salary_service)],
    left: uuid.UUID = Query(...),
    right: uuid.UUID = Query(...),
) -> SalaryCompareResponse:
    return SalaryCompareResponse(
        left_assignment_id=left,
        right_assignment_id=right,
        components=service.compare(context, employee_id, left, right),
    )


@router.post(
    "/employees/{employee_id}/salary-assignments/preview-timeline",
    response_model=PreviewResponse,
    summary="Dry-run a create/confirm",
)
def preview_timeline(
    employee_id: uuid.UUID,
    payload: SalaryAssignmentCreateRequest,
    context: CurrentOrg,
    service: Annotated[SalaryService, Depends(get_salary_service)],
) -> PreviewResponse:
    values = payload.model_dump()
    values["components"] = payload.components
    return PreviewResponse(**service.preview(context, employee_id, values))


@router.post(
    "/employees/{employee_id}/salary-assignments",
    response_model=SalaryAssignmentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create or create-and-confirm a salary revision",
)
def create_assignment(
    employee_id: uuid.UUID,
    payload: SalaryAssignmentCreateRequest,
    context: CurrentOrg,
    db: DbSession,
    service: Annotated[SalaryService, Depends(get_salary_service)],
) -> SalaryAssignmentResponse:
    values = payload.model_dump()
    values["components"] = payload.components
    with unit_of_work(db):
        row = service.create_assignment(context, employee_id, values)
        return _assignment_payload(service, row)


@router.patch(
    "/employees/{employee_id}/salary-assignments/{assignment_id}",
    response_model=SalaryAssignmentResponse,
    summary="Edit a draft assignment",
)
def update_assignment(
    employee_id: uuid.UUID,
    assignment_id: uuid.UUID,
    payload: SalaryAssignmentUpdateRequest,
    response: Response,
    context: CurrentOrg,
    if_match: IfMatch,
    db: DbSession,
    service: Annotated[SalaryService, Depends(get_salary_service)],
) -> SalaryAssignmentResponse:
    changes = payload.model_dump(exclude_unset=True)
    changes["components"] = payload.components
    with unit_of_work(db):
        row = service.update_assignment(
            context, employee_id, assignment_id, changes, if_match=if_match
        )
        result = _assignment_payload(service, row)
    response.headers["ETag"] = etag_for(row.row_version)
    return result


@router.delete(
    "/employees/{employee_id}/salary-assignments/{assignment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_draft(
    employee_id: uuid.UUID,
    assignment_id: uuid.UUID,
    context: CurrentOrg,
    db: DbSession,
    service: Annotated[SalaryService, Depends(get_salary_service)],
) -> Response:
    with unit_of_work(db):
        service.delete_draft(context, employee_id, assignment_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/employees/{employee_id}/salary-assignments/{assignment_id}/submit",
    response_model=SalaryAssignmentResponse,
)
def submit(
    employee_id: uuid.UUID,
    assignment_id: uuid.UUID,
    context: CurrentOrg,
    db: DbSession,
    service: Annotated[SalaryService, Depends(get_salary_service)],
) -> SalaryAssignmentResponse:
    with unit_of_work(db):
        row = service.submit(context, employee_id, assignment_id)
        return _assignment_payload(service, row)


@router.post(
    "/employees/{employee_id}/salary-assignments/{assignment_id}/approve",
    response_model=SalaryAssignmentResponse,
)
def approve(
    employee_id: uuid.UUID,
    assignment_id: uuid.UUID,
    payload: ApproveRequest,
    context: CurrentOrg,
    db: DbSession,
    service: Annotated[SalaryService, Depends(get_salary_service)],
) -> SalaryAssignmentResponse:
    with unit_of_work(db):
        row = service.approve(context, employee_id, assignment_id, confirm=payload.confirm)
        return _assignment_payload(service, row)


@router.post(
    "/employees/{employee_id}/salary-assignments/{assignment_id}/reject",
    response_model=SalaryAssignmentResponse,
)
def reject(
    employee_id: uuid.UUID,
    assignment_id: uuid.UUID,
    payload: RejectRequest,
    context: CurrentOrg,
    db: DbSession,
    service: Annotated[SalaryService, Depends(get_salary_service)],
) -> SalaryAssignmentResponse:
    with unit_of_work(db):
        row = service.reject(context, employee_id, assignment_id, payload.rejection_reason)
        return _assignment_payload(service, row)


@router.post(
    "/employees/{employee_id}/salary-assignments/{assignment_id}/confirm",
    response_model=SalaryAssignmentResponse,
    summary="Confirm a revision into the timeline (O1)",
)
def confirm(
    employee_id: uuid.UUID,
    assignment_id: uuid.UUID,
    payload: ConfirmRequest,
    context: CurrentOrg,
    db: DbSession,
    service: Annotated[SalaryService, Depends(get_salary_service)],
) -> SalaryAssignmentResponse:
    with unit_of_work(db):
        row = service.confirm(
            context,
            employee_id,
            assignment_id,
            backdate_reason=payload.backdate_reason,
        )
        return _assignment_payload(service, row)


@router.post(
    "/employees/{employee_id}/salary-assignments/{assignment_id}/cancel",
    response_model=SalaryAssignmentResponse,
    summary="Cancel a scheduled revision (O2)",
)
def cancel(
    employee_id: uuid.UUID,
    assignment_id: uuid.UUID,
    payload: ReasonRequest,
    context: CurrentOrg,
    db: DbSession,
    service: Annotated[SalaryService, Depends(get_salary_service)],
) -> SalaryAssignmentResponse:
    with unit_of_work(db):
        row = service.cancel(context, employee_id, assignment_id, payload.reason)
        return _assignment_payload(service, row)


@router.post(
    "/employees/{employee_id}/salary-assignments/{assignment_id}/replace",
    response_model=SalaryAssignmentResponse,
    summary="Replace a scheduled revision atomically",
)
def replace(
    employee_id: uuid.UUID,
    assignment_id: uuid.UUID,
    payload: ReplaceRequest,
    context: CurrentOrg,
    db: DbSession,
    service: Annotated[SalaryService, Depends(get_salary_service)],
) -> SalaryAssignmentResponse:
    values = payload.model_dump()
    values["components"] = payload.components
    with unit_of_work(db):
        row = service.replace(context, employee_id, assignment_id, values)
        return _assignment_payload(service, row)


@router.post(
    "/employees/{employee_id}/salary-assignments/{assignment_id}/correct",
    response_model=SalaryAssignmentResponse,
    summary="Correct an ACTIVE/EXPIRED assignment (O3)",
)
def correct(
    employee_id: uuid.UUID,
    assignment_id: uuid.UUID,
    payload: CorrectRequest,
    context: CurrentOrg,
    db: DbSession,
    service: Annotated[SalaryService, Depends(get_salary_service)],
) -> SalaryAssignmentResponse:
    with unit_of_work(db):
        row = service.correct(
            context,
            employee_id,
            assignment_id,
            components=payload.components,
            reason=payload.reason,
        )
        return _assignment_payload(service, row)


@router.get(
    "/employees/{employee_id}/salary-history",
    response_model=list[SalaryHistoryResponse],
    summary="Read salary revision events",
)
def salary_history(
    employee_id: uuid.UUID,
    context: CurrentOrg,
    service: Annotated[SalaryService, Depends(get_salary_service)],
) -> list[SalaryHistoryResponse]:
    return [
        SalaryHistoryResponse.model_validate(row) for row in service.history(context, employee_id)
    ]


__all__ = ["router"]
