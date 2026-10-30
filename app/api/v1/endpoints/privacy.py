"""Privacy notices, exports, erasure, legal hold and retention batches (§25)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Response, status

from app.api.deps import CurrentOrg, DbSession, get_employee_service, get_privacy_service
from app.db.session import unit_of_work
from app.schemas.common import etag_for
from app.schemas.employee import EmployeeAddressesResponse, EmployeeResponse
from app.schemas.privacy import (
    ErasureCreateRequest,
    ErasureDecisionRequest,
    ErasureResponse,
    ExportRequest,
    ExportResponse,
    LegalHoldRequest,
    NoticeCreateRequest,
    NoticeResponse,
    NoticeWithdrawRequest,
    RetentionBatchResponse,
    RetentionProposeRequest,
)
from app.services.employees import EmployeeService
from app.services.privacy import PrivacyService

router = APIRouter(prefix="/orgs/{org_id}", tags=["Privacy"])


@router.get(
    "/employees/{employee_id}/privacy-notices",
    response_model=list[NoticeResponse],
    summary="List privacy notice and consent records",
)
def list_notices(
    employee_id: uuid.UUID,
    context: CurrentOrg,
    service: Annotated[PrivacyService, Depends(get_privacy_service)],
) -> list[NoticeResponse]:
    return [
        NoticeResponse.model_validate(row) for row in service.list_notices(context, employee_id)
    ]


@router.post(
    "/employees/{employee_id}/privacy-notices",
    response_model=NoticeResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Record a notice delivery or consent",
)
def create_notice(
    employee_id: uuid.UUID,
    payload: NoticeCreateRequest,
    context: CurrentOrg,
    db: DbSession,
    service: Annotated[PrivacyService, Depends(get_privacy_service)],
) -> NoticeResponse:
    with unit_of_work(db):
        row = service.create_notice(context, employee_id, payload.model_dump())
    return NoticeResponse.model_validate(row)


@router.post(
    "/employees/{employee_id}/privacy-notices/{notice_id}/acknowledge",
    response_model=NoticeResponse,
    summary="Acknowledge a notice",
)
def acknowledge_notice(
    employee_id: uuid.UUID,
    notice_id: uuid.UUID,
    context: CurrentOrg,
    db: DbSession,
    service: Annotated[PrivacyService, Depends(get_privacy_service)],
) -> NoticeResponse:
    with unit_of_work(db):
        row = service.acknowledge_notice(context, employee_id, notice_id)
    return NoticeResponse.model_validate(row)


@router.post(
    "/employees/{employee_id}/privacy-notices/{notice_id}/withdraw",
    response_model=NoticeResponse,
    summary="Withdraw consent",
)
def withdraw_notice(
    employee_id: uuid.UUID,
    notice_id: uuid.UUID,
    payload: NoticeWithdrawRequest,
    context: CurrentOrg,
    db: DbSession,
    service: Annotated[PrivacyService, Depends(get_privacy_service)],
) -> NoticeResponse:
    with unit_of_work(db):
        row = service.withdraw_notice(context, employee_id, notice_id, payload.reason)
    return NoticeResponse.model_validate(row)


@router.post(
    "/employees/{employee_id}/legal-hold",
    response_model=EmployeeResponse,
    summary="Set or clear the legal hold",
)
def set_legal_hold(
    employee_id: uuid.UUID,
    payload: LegalHoldRequest,
    response: Response,
    context: CurrentOrg,
    db: DbSession,
    privacy: Annotated[PrivacyService, Depends(get_privacy_service)],
    employees: Annotated[EmployeeService, Depends(get_employee_service)],
) -> EmployeeResponse:
    with unit_of_work(db):
        employee = privacy.set_legal_hold(
            context, employee_id, hold=payload.legal_hold, reason=payload.reason
        )
        result = EmployeeResponse.model_validate(employee)
        result.addresses = EmployeeAddressesResponse(**employees.addresses(context, employee.id))
    response.headers["ETag"] = etag_for(employee.row_version)
    return result


@router.post(
    "/employees/{employee_id}/data-export",
    response_model=ExportResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Generate an encrypted, expiring data export",
)
def export_employee(
    employee_id: uuid.UUID,
    payload: ExportRequest,
    context: CurrentOrg,
    db: DbSession,
    service: Annotated[PrivacyService, Depends(get_privacy_service)],
) -> ExportResponse:
    with unit_of_work(db):
        export_id, url, expires, password, masked = service.export_employee(
            context,
            employee_id,
            unmasked=payload.unmasked,
            include_documents=payload.include_documents,
        )
    return ExportResponse(
        export_id=export_id,
        download_url=url,
        expires_at=expires,
        one_time_password=password,
        masked=masked,
    )


@router.get(
    "/employees/{employee_id}/erasure-requests",
    response_model=list[ErasureResponse],
    summary="List erasure requests",
)
def list_erasures(
    employee_id: uuid.UUID,
    context: CurrentOrg,
    service: Annotated[PrivacyService, Depends(get_privacy_service)],
) -> list[ErasureResponse]:
    return [
        ErasureResponse.model_validate(row) for row in service.list_erasures(context, employee_id)
    ]


@router.post(
    "/employees/{employee_id}/erasure-requests",
    response_model=ErasureResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Record an erasure request",
)
def create_erasure(
    employee_id: uuid.UUID,
    payload: ErasureCreateRequest,
    context: CurrentOrg,
    db: DbSession,
    service: Annotated[PrivacyService, Depends(get_privacy_service)],
) -> ErasureResponse:
    with unit_of_work(db):
        row = service.create_erasure(context, employee_id, payload.requester_note)
    return ErasureResponse.model_validate(row)


@router.post(
    "/employees/{employee_id}/erasure-requests/{request_id}/decision",
    response_model=ErasureResponse,
    summary="Approve (and execute) or refuse an erasure request",
)
def decide_erasure(
    employee_id: uuid.UUID,
    request_id: uuid.UUID,
    payload: ErasureDecisionRequest,
    context: CurrentOrg,
    db: DbSession,
    service: Annotated[PrivacyService, Depends(get_privacy_service)],
) -> ErasureResponse:
    with unit_of_work(db):
        row = service.decide_erasure(
            context,
            employee_id,
            request_id,
            decision=payload.decision,
            reason=payload.reason,
        )
    return ErasureResponse.model_validate(row)


@router.get(
    "/retention-batches",
    response_model=list[RetentionBatchResponse],
    summary="List retention purge batches",
)
def list_batches(
    context: CurrentOrg,
    service: Annotated[PrivacyService, Depends(get_privacy_service)],
) -> list[RetentionBatchResponse]:
    return [RetentionBatchResponse.model_validate(row) for row in service.list_batches(context)]


@router.post(
    "/retention-batches",
    response_model=RetentionBatchResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Propose a retention purge batch",
)
def propose_batch(
    payload: RetentionProposeRequest,
    context: CurrentOrg,
    db: DbSession,
    service: Annotated[PrivacyService, Depends(get_privacy_service)],
) -> RetentionBatchResponse:
    with unit_of_work(db):
        row = service.propose_batch(context, payload.scope_description, payload.affected_entity_ids)
    return RetentionBatchResponse.model_validate(row)


@router.post(
    "/retention-batches/{batch_id}/approve",
    response_model=RetentionBatchResponse,
    summary="Approve a batch with a second approver",
)
def approve_batch(
    batch_id: uuid.UUID,
    context: CurrentOrg,
    db: DbSession,
    service: Annotated[PrivacyService, Depends(get_privacy_service)],
) -> RetentionBatchResponse:
    with unit_of_work(db):
        row = service.approve_batch(context, batch_id)
    return RetentionBatchResponse.model_validate(row)


@router.post(
    "/retention-batches/{batch_id}/execute",
    response_model=RetentionBatchResponse,
    summary="Execute an approved retention batch",
)
def execute_batch(
    batch_id: uuid.UUID,
    context: CurrentOrg,
    db: DbSession,
    service: Annotated[PrivacyService, Depends(get_privacy_service)],
) -> RetentionBatchResponse:
    with unit_of_work(db):
        service.execute_batch(context, batch_id)
        row = service._batches.require(batch_id, resource="retention batch")
    return RetentionBatchResponse.model_validate(row)


__all__ = ["router"]
