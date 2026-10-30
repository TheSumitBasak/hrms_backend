"""Employee identifiers and duplicate pre-check (§24.4.4)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status

from app.api.deps import CurrentOrg, DbSession, get_identifier_service
from app.db.session import unit_of_work
from app.schemas.identifier import (
    DuplicateCheckRequest,
    DuplicateCheckResponse,
    DuplicateMatch,
    IdentifierPutRequest,
    IdentifierResponse,
    IdentifierRevealRequest,
    IdentifierRevealResponse,
)
from app.services.identifiers import IdentifierService

router = APIRouter(prefix="/orgs/{org_id}", tags=["Identifiers"])


@router.get(
    "/employees/{employee_id}/identifiers",
    response_model=list[IdentifierResponse],
    summary="List masked employee identifiers",
)
def list_identifiers(
    employee_id: uuid.UUID,
    context: CurrentOrg,
    service: Annotated[IdentifierService, Depends(get_identifier_service)],
) -> list[IdentifierResponse]:
    return service.list_identifiers(context, employee_id)


@router.put(
    "/employees/{employee_id}/identifiers/{identifier_type}",
    response_model=IdentifierResponse,
    status_code=status.HTTP_200_OK,
    summary="Add or replace an identifier",
)
def put_identifier(
    employee_id: uuid.UUID,
    identifier_type: str,
    payload: IdentifierPutRequest,
    context: CurrentOrg,
    db: DbSession,
    service: Annotated[IdentifierService, Depends(get_identifier_service)],
) -> IdentifierResponse:
    with unit_of_work(db):
        service.put_identifier(context, employee_id, identifier_type, payload.model_dump())
        return next(
            item
            for item in service.list_identifiers(context, employee_id)
            if item.identifier_type == identifier_type and item.is_current
        )


@router.post(
    "/employees/{employee_id}/identifiers/{identifier_type}/reveal",
    response_model=IdentifierRevealResponse,
    summary="Reveal an identifier (audited and rate-limited)",
)
def reveal_identifier(
    employee_id: uuid.UUID,
    identifier_type: str,
    payload: IdentifierRevealRequest,
    context: CurrentOrg,
    db: DbSession,
    service: Annotated[IdentifierService, Depends(get_identifier_service)],
) -> IdentifierRevealResponse:
    with unit_of_work(db):
        value = service.reveal(context, employee_id, identifier_type, payload.reason)
    return IdentifierRevealResponse(identifier_type=identifier_type, value=value)


@router.post(
    "/identifier-duplicate-check",
    response_model=DuplicateCheckResponse,
    summary="Check whether an identifier is already recorded",
)
def duplicate_check(
    payload: DuplicateCheckRequest,
    context: CurrentOrg,
    db: DbSession,
    service: Annotated[IdentifierService, Depends(get_identifier_service)],
) -> DuplicateCheckResponse:
    with unit_of_work(db):
        matches = service.duplicate_check(context, payload.identifier_type, payload.value)
    return DuplicateCheckResponse(matches=[DuplicateMatch(**match) for match in matches])


__all__ = ["router"]
