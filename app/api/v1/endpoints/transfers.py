"""Inter-organization transfer (§10.9)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status

from app.api.deps import (
    CurrentOrg,
    CurrentUser,
    DbSession,
    get_access_service,
    get_employee_repository,
    get_transfer_service,
)
from app.core.errors import PermissionDenied, ResourceNotFound
from app.core.permissions import Permission
from app.db.session import unit_of_work
from app.repositories.employee import EmployeeRepository
from app.schemas.employee import EmployeeResponse, TransferRequest
from app.services.access import AccessService
from app.services.transfers import TransferService

router = APIRouter(prefix="/orgs/{org_id}/employees", tags=["Transfers"])


@router.post(
    "/{source_employee_id}/transfers",
    response_model=EmployeeResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Transfer an employee from another organization into this one",
)
def transfer_employee(
    source_employee_id: uuid.UUID,
    payload: TransferRequest,
    context: CurrentOrg,
    actor: CurrentUser,
    db: DbSession,
    employees: Annotated[EmployeeRepository, Depends(get_employee_repository)],
    access: Annotated[AccessService, Depends(get_access_service)],
    service: Annotated[TransferService, Depends(get_transfer_service)],
) -> EmployeeResponse:
    source = employees.get_any_org(source_employee_id)
    if source is None:
        raise ResourceNotFound("employee")
    entries = {entry.organization_id: entry for entry in access.organization_access(actor.user_id)}
    entry = entries.get(source.organization_id)
    if entry is None or not entry.permissions.has(Permission.EMPLOYEE_STATUS):
        raise PermissionDenied("The caller needs employee.status in the source organization.")
    values = payload.model_dump()
    with unit_of_work(db):
        employee = service.transfer(context, source_employee_id=source_employee_id, values=values)
        return EmployeeResponse.model_validate(employee)


__all__ = ["router"]
