"""Employee bank accounts (§24.4.4)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Response, status

from app.api.deps import CurrentOrg, DbSession, IfMatch, get_bank_account_service
from app.db.session import unit_of_work
from app.schemas.bank import (
    BankAccountCreateRequest,
    BankAccountPrimaryRequest,
    BankAccountResponse,
    BankAccountRevealRequest,
    BankAccountRevealResponse,
    BankAccountUpdateRequest,
    BankAccountVerifyRequest,
)
from app.schemas.common import etag_for
from app.services.bank import BankAccountService

router = APIRouter(prefix="/orgs/{org_id}/employees/{employee_id}/bank-accounts", tags=["Bank"])


@router.get("", response_model=list[BankAccountResponse], summary="List masked bank accounts")
def list_accounts(
    employee_id: uuid.UUID,
    context: CurrentOrg,
    service: Annotated[BankAccountService, Depends(get_bank_account_service)],
) -> list[BankAccountResponse]:
    return service.list_accounts(context, employee_id)


@router.post(
    "",
    response_model=BankAccountResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Add a bank account",
)
def add_account(
    employee_id: uuid.UUID,
    payload: BankAccountCreateRequest,
    context: CurrentOrg,
    db: DbSession,
    service: Annotated[BankAccountService, Depends(get_bank_account_service)],
) -> BankAccountResponse:
    with unit_of_work(db):
        row, _ = service.add_account(
            context,
            employee_id,
            payload.model_dump(exclude={"acknowledged_warnings"}),
            payload.acknowledged_warnings,
        )
        return next(
            item for item in service.list_accounts(context, employee_id) if item.id == row.id
        )


@router.patch(
    "/{account_id}", response_model=BankAccountResponse, summary="Edit or close an account"
)
def update_account(
    employee_id: uuid.UUID,
    account_id: uuid.UUID,
    payload: BankAccountUpdateRequest,
    response: Response,
    context: CurrentOrg,
    if_match: IfMatch,
    db: DbSession,
    service: Annotated[BankAccountService, Depends(get_bank_account_service)],
) -> BankAccountResponse:
    with unit_of_work(db):
        row = service.update_account(
            context,
            employee_id,
            account_id,
            changes=payload.model_dump(exclude_unset=True),
            if_match=if_match,
        )
        response.headers["ETag"] = etag_for(row.row_version)
        return next(
            item for item in service.list_accounts(context, employee_id) if item.id == row.id
        )


@router.post(
    "/{account_id}/set-primary",
    response_model=BankAccountResponse,
    summary="Set the primary account",
)
def set_primary(
    employee_id: uuid.UUID,
    account_id: uuid.UUID,
    payload: BankAccountPrimaryRequest,
    context: CurrentOrg,
    db: DbSession,
    service: Annotated[BankAccountService, Depends(get_bank_account_service)],
) -> BankAccountResponse:
    with unit_of_work(db):
        row = service.set_primary(
            context, employee_id, account_id, effective_from=payload.effective_from
        )
        return next(
            item for item in service.list_accounts(context, employee_id) if item.id == row.id
        )


@router.post(
    "/{account_id}/verify", response_model=BankAccountResponse, summary="Verify an account"
)
def verify_account(
    employee_id: uuid.UUID,
    account_id: uuid.UUID,
    payload: BankAccountVerifyRequest,
    context: CurrentOrg,
    db: DbSession,
    service: Annotated[BankAccountService, Depends(get_bank_account_service)],
) -> BankAccountResponse:
    with unit_of_work(db):
        row = service.verify(
            context,
            employee_id,
            account_id,
            status=payload.status,
            verification_date=payload.verification_date,
            remarks=payload.remarks,
            bank_proof_document_id=payload.bank_proof_document_id,
        )
        return next(
            item for item in service.list_accounts(context, employee_id) if item.id == row.id
        )


@router.post(
    "/{account_id}/reveal",
    response_model=BankAccountRevealResponse,
    summary="Reveal the full account number (audited)",
)
def reveal_account(
    employee_id: uuid.UUID,
    account_id: uuid.UUID,
    payload: BankAccountRevealRequest,
    context: CurrentOrg,
    db: DbSession,
    service: Annotated[BankAccountService, Depends(get_bank_account_service)],
) -> BankAccountRevealResponse:
    with unit_of_work(db):
        value = service.reveal(context, employee_id, account_id, payload.reason)
    return BankAccountRevealResponse(account_number=value)


__all__ = ["router"]
