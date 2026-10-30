"""Employees, status transitions, job history and code reassignment (§24.4.3)."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Response, status

from app.api.deps import (
    CurrentOrg,
    DbSession,
    IfMatch,
    get_employee_code_service,
    get_employee_service,
    get_identifier_service,
    get_position_assignment_service,
)
from app.core.clock import today_in
from app.core.context import OrgContext
from app.core.errors import ValidationFailed
from app.core.permissions import Permission
from app.db.session import unit_of_work
from app.models.employee import Employee
from app.schemas.common import Page, etag_for
from app.schemas.employee import (
    AgeExceptionDecisionRequest,
    ArchiveRequest,
    EmployeeAddressesResponse,
    EmployeeCreateRequest,
    EmployeeResponse,
    EmployeeSearchResponse,
    EmployeeSummary,
    EmployeeUpdateRequest,
    JobChangeRequest,
    JobHistoryResponse,
    StatusHistoryResponse,
    StatusTransitionRequest,
)
from app.schemas.id_rules import CodeReassignmentRequest, CodeRegistryResponse
from app.schemas.position import EmployeePositionAssignmentResponse
from app.services.employee_codes import EmployeeCodeService
from app.services.employees import EmployeeService
from app.services.identifiers import IdentifierService
from app.services.position_assignments import PositionAssignmentService

router = APIRouter(prefix="/orgs/{org_id}/employees", tags=["Employees"])


def _etag(response: Response, row_version: int) -> None:
    response.headers["ETag"] = etag_for(row_version)


def _employee_response(
    context: OrgContext,
    employee: Employee,
    addresses: dict[str, Any] | None = None,
) -> EmployeeResponse:
    sensitive = context.has(Permission.SENSITIVE_RESTRICTED_PERSONAL)
    payload = EmployeeResponse.model_validate(employee)
    if not sensitive:
        payload.religion = None
        payload.social_category = None
        payload.disability_type = None
        payload.disability_percentage = None
        payload.father_name = None
        payload.mother_name = None
        payload.spouse_name = None
    if addresses is not None:
        payload.addresses = EmployeeAddressesResponse(**addresses)
    return payload


@router.get("", response_model=EmployeeSearchResponse, summary="Search and list employees")
def list_employees(
    context: CurrentOrg,
    service: Annotated[EmployeeService, Depends(get_employee_service)],
    identifiers: Annotated[IdentifierService, Depends(get_identifier_service)],
    q: Annotated[str | None, Query(min_length=2, max_length=100)] = None,
    employee_code: str | None = None,
    official_email: str | None = None,
    status_filter: Annotated[list[str] | None, Query(alias="status")] = None,
    department_id: Annotated[list[uuid.UUID] | None, Query()] = None,
    designation_id: Annotated[list[uuid.UUID] | None, Query()] = None,
    branch_id: Annotated[list[uuid.UUID] | None, Query()] = None,
    employment_type_id: Annotated[list[uuid.UUID] | None, Query()] = None,
    grade_id: Annotated[list[uuid.UUID] | None, Query()] = None,
    business_unit_id: Annotated[list[uuid.UUID] | None, Query()] = None,
    cost_center_id: Annotated[list[uuid.UUID] | None, Query()] = None,
    work_location_id: Annotated[list[uuid.UUID] | None, Query()] = None,
    employee_category_id: Annotated[list[uuid.UUID] | None, Query()] = None,
    work_mode: Annotated[list[str] | None, Query()] = None,
    joining_date_from: date | None = None,
    joining_date_to: date | None = None,
    reporting_manager_employee_id: uuid.UUID | None = None,
    include_indirect: bool = False,
    identifier_type: str | None = None,
    identifier_value: str | None = None,
    include_archived: bool = False,
    sort: str | None = None,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    with_total: bool = False,
) -> EmployeeSearchResponse:
    employee_ids = None
    if identifier_type is not None or identifier_value is not None:
        if identifier_type is None or identifier_value is None:
            raise ValidationFailed("identifier_type and identifier_value are both required.")
        employee_ids = identifiers.search_employees(context, identifier_type, identifier_value)
        if not employee_ids:
            return EmployeeSearchResponse(items=[], next_cursor=None, total_count=0)
    items, next_cursor, total = service.search(
        context,
        query=q,
        employee_code=employee_code,
        official_email=official_email,
        status=status_filter,
        department_ids=department_id,
        designation_ids=designation_id,
        branch_ids=branch_id,
        employment_type_ids=employment_type_id,
        grade_ids=grade_id,
        business_unit_ids=business_unit_id,
        cost_center_ids=cost_center_id,
        work_location_ids=work_location_id,
        employee_category_ids=employee_category_id,
        work_modes=work_mode,
        joining_date_from=joining_date_from,
        joining_date_to=joining_date_to,
        reporting_manager_employee_id=reporting_manager_employee_id,
        include_indirect=include_indirect,
        employee_ids=employee_ids,
        include_archived=include_archived,
        sort=sort,
        cursor=cursor,
        limit=limit,
        with_total=with_total,
    )
    return EmployeeSearchResponse(
        items=[EmployeeSummary(**item) for item in items],
        next_cursor=next_cursor,
        total_count=total,
    )


@router.post(
    "",
    response_model=EmployeeResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a draft employee",
)
def create_employee(
    payload: EmployeeCreateRequest,
    response: Response,
    context: CurrentOrg,
    db: DbSession,
    service: Annotated[EmployeeService, Depends(get_employee_service)],
) -> EmployeeResponse:
    values = payload.model_dump(exclude={"acknowledged_warnings"})
    manual_code = values.pop("employee_code", None)
    with unit_of_work(db):
        employee, _ = service.create(
            context,
            values=values,
            acknowledged_warnings=payload.acknowledged_warnings,
            manual_code=manual_code,
        )
        addresses = service.addresses(context, employee.id)
        result = _employee_response(context, employee, addresses)
    _etag(response, employee.row_version)
    return result


@router.get("/{employee_id}", response_model=EmployeeResponse, summary="Read an employee")
def read_employee(
    employee_id: uuid.UUID,
    response: Response,
    context: CurrentOrg,
    service: Annotated[EmployeeService, Depends(get_employee_service)],
) -> EmployeeResponse:
    employee = service.get(context, employee_id)
    _etag(response, employee.row_version)
    return _employee_response(context, employee, service.addresses(context, employee_id))


@router.patch(
    "/{employee_id}", response_model=EmployeeResponse, summary="Edit personal and contact data"
)
def update_employee(
    employee_id: uuid.UUID,
    payload: EmployeeUpdateRequest,
    response: Response,
    context: CurrentOrg,
    if_match: IfMatch,
    db: DbSession,
    service: Annotated[EmployeeService, Depends(get_employee_service)],
) -> EmployeeResponse:
    changes = payload.model_dump(exclude_unset=True, exclude={"acknowledged_warnings"})
    addresses = {
        "current_address": changes.pop("current_address", None),
        "permanent_address": changes.pop("permanent_address", None),
        "permanent_address_same_as_current": changes.pop("permanent_address_same_as_current", None),
    }
    with unit_of_work(db):
        employee, _ = service.update(
            context,
            employee_id=employee_id,
            changes=changes,
            addresses=addresses,
            if_match=if_match,
            acknowledged_warnings=payload.acknowledged_warnings,
        )
        result = _employee_response(context, employee, service.addresses(context, employee_id))
    _etag(response, employee.row_version)
    return result


@router.post("/{employee_id}/activate", response_model=EmployeeResponse, summary="Activate a draft")
def activate_employee(
    employee_id: uuid.UUID,
    response: Response,
    context: CurrentOrg,
    db: DbSession,
    service: Annotated[EmployeeService, Depends(get_employee_service)],
) -> EmployeeResponse:
    with unit_of_work(db):
        employee = service.activate(context, employee_id=employee_id)
        result = _employee_response(context, employee, service.addresses(context, employee_id))
    _etag(response, employee.row_version)
    return result


@router.post(
    "/{employee_id}/status-transitions",
    response_model=EmployeeResponse,
    summary="Change employee status",
)
def transition_status(
    employee_id: uuid.UUID,
    payload: StatusTransitionRequest,
    response: Response,
    context: CurrentOrg,
    db: DbSession,
    service: Annotated[EmployeeService, Depends(get_employee_service)],
) -> EmployeeResponse:
    with unit_of_work(db):
        employee = service.transition_status(
            context,
            employee_id=employee_id,
            to_status=payload.to_status,
            effective_date=payload.status_effective_date,
            employment_end_date=payload.employment_end_date,
            status_reason=payload.status_reason,
            remarks=payload.remarks,
            is_correction=payload.is_correction,
        )
        result = _employee_response(context, employee, service.addresses(context, employee_id))
    _etag(response, employee.row_version)
    return result


@router.get(
    "/{employee_id}/status-history",
    response_model=list[StatusHistoryResponse],
    summary="Read status history",
)
def status_history(
    employee_id: uuid.UUID,
    context: CurrentOrg,
    service: Annotated[EmployeeService, Depends(get_employee_service)],
) -> list[StatusHistoryResponse]:
    return [
        StatusHistoryResponse.model_validate(row)
        for row in service.status_history(context, employee_id)
    ]


@router.post(
    "/{employee_id}/job-changes",
    response_model=EmployeeResponse,
    summary="Record an effective-dated job change",
)
def change_job(
    employee_id: uuid.UUID,
    payload: JobChangeRequest,
    response: Response,
    context: CurrentOrg,
    db: DbSession,
    service: Annotated[EmployeeService, Depends(get_employee_service)],
) -> EmployeeResponse:
    """§24.4.3, extended additively by JA §7.7.

    An optional ``position_change`` object moves the employee between positions in the same
    transaction as the job change. Omitting it leaves the behaviour and the response exactly
    as they were before the job-architecture module (JA §20.7).
    """
    with unit_of_work(db):
        employee, _history, assignment = service.change_job(
            context, employee_id=employee_id, changes=payload.model_dump(exclude_unset=True)
        )
        result = _employee_response(context, employee, service.addresses(context, employee_id))
        if assignment is not None:
            result.position_assignment = EmployeePositionAssignmentResponse.model_validate(
                assignment
            )
    _etag(response, employee.row_version)
    return result


@router.get(
    "/{employee_id}/position-assignments",
    response_model=Page[EmployeePositionAssignmentResponse],
    summary="Read an employee's position assignment history",
)
def position_assignments(
    employee_id: uuid.UUID,
    context: CurrentOrg,
    service: Annotated[PositionAssignmentService, Depends(get_position_assignment_service)],
    status_filter: Annotated[list[str] | None, Query(alias="status")] = None,
    as_of: date | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
) -> Page[EmployeePositionAssignmentResponse]:
    """JA §13.7. Needs ``employee.view`` *and* ``position.view``: the row joins the two."""
    rows = service.list_for_employee(context, employee_id, status=status_filter, as_of=as_of)
    items = [
        EmployeePositionAssignmentResponse(
            id=assignment.id,
            employee_id=assignment.employee_id,
            position_id=assignment.position_id,
            position_code=position.position_code if position else None,
            position_title=position.position_title if position else None,
            assignment_type=assignment.assignment_type,
            assignment_status=assignment.assignment_status,
            fte=assignment.fte,
            effective_from=assignment.effective_from,
            effective_to=assignment.effective_to,
            end_reason=assignment.end_reason,
            source=assignment.source,
            row_version=assignment.row_version,
        )
        for assignment, position in rows[:limit]
    ]
    return Page[EmployeePositionAssignmentResponse](items=items)


@router.get(
    "/{employee_id}/job-history",
    response_model=list[JobHistoryResponse],
    summary="Read job history",
)
def job_history(
    employee_id: uuid.UUID,
    context: CurrentOrg,
    service: Annotated[EmployeeService, Depends(get_employee_service)],
) -> list[JobHistoryResponse]:
    return [
        JobHistoryResponse.model_validate(row) for row in service.job_history(context, employee_id)
    ]


@router.post(
    "/{employee_id}/age-exception-decision",
    response_model=EmployeeResponse,
    summary="Approve or reject a pending age exception",
)
def decide_age_exception(
    employee_id: uuid.UUID,
    payload: AgeExceptionDecisionRequest,
    response: Response,
    context: CurrentOrg,
    db: DbSession,
    service: Annotated[EmployeeService, Depends(get_employee_service)],
) -> EmployeeResponse:
    with unit_of_work(db):
        employee = service.decide_age_exception(
            context,
            employee_id=employee_id,
            decision=payload.decision,
            reason=payload.reason,
        )
        result = _employee_response(context, employee, service.addresses(context, employee_id))
    _etag(response, employee.row_version)
    return result


@router.post(
    "/{employee_id}/archive", response_model=EmployeeResponse, summary="Archive an employee"
)
def archive_employee(
    employee_id: uuid.UUID,
    payload: ArchiveRequest,
    response: Response,
    context: CurrentOrg,
    db: DbSession,
    service: Annotated[EmployeeService, Depends(get_employee_service)],
) -> EmployeeResponse:
    with unit_of_work(db):
        employee = service.archive(context, employee_id=employee_id, reason=payload.reason)
        result = _employee_response(context, employee)
    _etag(response, employee.row_version)
    return result


@router.post(
    "/{employee_id}/code-reassignment",
    response_model=EmployeeResponse,
    summary="Reassign an employee code",
)
def reassign_code(
    employee_id: uuid.UUID,
    payload: CodeReassignmentRequest,
    response: Response,
    context: CurrentOrg,
    db: DbSession,
    _: Annotated[EmployeeService, Depends(get_employee_service)],
    codes: Annotated[EmployeeCodeService, Depends(get_employee_code_service)],
) -> EmployeeResponse:
    with unit_of_work(db):
        employee, _new_code = codes.reassign_code(
            context,
            employee_id=employee_id,
            mode=payload.mode,
            new_code=payload.new_code,
            reason=payload.reason,
            basis_date=today_in(None),
        )
        result = _employee_response(context, employee)
    _etag(response, employee.row_version)
    return result


@router.get(
    "/{employee_id}/code-history",
    response_model=list[CodeRegistryResponse],
    summary="Read codes issued to an employee",
)
def code_history(
    employee_id: uuid.UUID,
    context: CurrentOrg,
    codes: Annotated[EmployeeCodeService, Depends(get_employee_code_service)],
) -> list[CodeRegistryResponse]:
    return [
        CodeRegistryResponse.model_validate(row) for row in codes.code_history(context, employee_id)
    ]


__all__ = ["router"]
