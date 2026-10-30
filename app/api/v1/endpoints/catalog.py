"""Branches, masters, lookups and custom statuses (§24.4.2)."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Response, status

from app.api.deps import (
    CurrentOrg,
    DbSession,
    IfMatch,
    get_catalogue_service,
    get_registration_repository,
)
from app.db.session import unit_of_work
from app.models.branch import Branch
from app.repositories.organization import OrganizationRegistrationRepository
from app.schemas.catalog import (
    BranchCreateRequest,
    BranchResponse,
    BranchStatusRequest,
    BranchUpdateRequest,
    CustomStatusCreateRequest,
    CustomStatusResponse,
    CustomStatusUpdateRequest,
    LookupCreateRequest,
    LookupResponse,
    LookupUpdateRequest,
    MasterCreateRequest,
    MasterResponse,
    MasterStatusRequest,
    MasterUpdateRequest,
)
from app.schemas.common import etag_for
from app.services.catalog import CatalogueService

router = APIRouter(prefix="/orgs/{org_id}", tags=["Branches and masters"])


def _etag(response: Response, row_version: int) -> None:
    response.headers["ETag"] = etag_for(row_version)


def _branch_response(branch: Branch, registrations: list[Any]) -> BranchResponse:
    """Attach the three §8.1 derived registration numbers.

    ``gstin`` resolves to the branch's own GSTIN first, then to an organization-level GSTIN
    whose state matches the branch — the spec's documented fallback.
    """
    own = {
        row.registration_type: row.registration_number
        for row in registrations
        if row.branch_id == branch.id and row.status == "ACTIVE"
    }
    gstin = own.get("GSTIN")
    if gstin is None:
        for row in registrations:
            if (
                row.registration_type == "GSTIN"
                and row.branch_id is None
                and row.status == "ACTIVE"
                and row.state_code == branch.state_code
            ):
                gstin = row.registration_number
                break
    return BranchResponse(
        id=branch.id,
        organization_id=branch.organization_id,
        branch_code=branch.branch_code,
        branch_name=branch.branch_name,
        address_line_1=branch.address_line_1,
        address_line_2=branch.address_line_2,
        city=branch.city,
        district=branch.district,
        state=branch.state,
        state_code=branch.state_code,
        country=branch.country,
        postal_code=branch.postal_code,
        official_phone=branch.official_phone,
        official_email=branch.official_email,
        status=branch.status,
        row_version=branch.row_version,
        gstin=gstin,
        shops_establishments_registration_number=own.get("SHOPS_ESTABLISHMENTS"),
        factory_license_number=own.get("FACTORY_LICENSE"),
    )


def _branch_responses(
    registrations: OrganizationRegistrationRepository,
    branches: list[Branch],
) -> list[BranchResponse]:
    rows = list(registrations.list_registrations())
    return [_branch_response(branch, rows) for branch in branches]


# --------------------------------------------------------------------------- branches


@router.get("/branches", response_model=list[BranchResponse], summary="List branches")
def list_branches(
    context: CurrentOrg,
    service: Annotated[CatalogueService, Depends(get_catalogue_service)],
    registrations: Annotated[
        OrganizationRegistrationRepository, Depends(get_registration_repository)
    ],
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    q: Annotated[str | None, Query(min_length=2, max_length=100)] = None,
) -> list[BranchResponse]:
    rows = service.list_branches(context, status=status_filter, query=q)
    return _branch_responses(registrations, list(rows))


@router.post(
    "/branches",
    response_model=BranchResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a branch",
)
def create_branch(
    payload: BranchCreateRequest,
    context: CurrentOrg,
    db: DbSession,
    service: Annotated[CatalogueService, Depends(get_catalogue_service)],
    registrations: Annotated[
        OrganizationRegistrationRepository, Depends(get_registration_repository)
    ],
) -> BranchResponse:
    values = payload.model_dump(mode="json", exclude={"acknowledged_warnings"}, exclude_none=True)
    with unit_of_work(db):
        branch, _ = service.create_branch(
            context,
            values=values,
            acknowledged_warnings=payload.acknowledged_warnings,
        )
        return _branch_response(branch, list(registrations.list_registrations()))


@router.get("/branches/{branch_id}", response_model=BranchResponse, summary="Read a branch")
def read_branch(
    branch_id: uuid.UUID,
    response: Response,
    context: CurrentOrg,
    service: Annotated[CatalogueService, Depends(get_catalogue_service)],
    registrations: Annotated[
        OrganizationRegistrationRepository, Depends(get_registration_repository)
    ],
) -> BranchResponse:
    branch = service.get_branch(context, branch_id)
    _etag(response, branch.row_version)
    return _branch_response(branch, list(registrations.list_registrations()))


@router.patch("/branches/{branch_id}", response_model=BranchResponse, summary="Edit a branch")
def update_branch(
    branch_id: uuid.UUID,
    payload: BranchUpdateRequest,
    response: Response,
    context: CurrentOrg,
    if_match: IfMatch,
    db: DbSession,
    service: Annotated[CatalogueService, Depends(get_catalogue_service)],
    registrations: Annotated[
        OrganizationRegistrationRepository, Depends(get_registration_repository)
    ],
) -> BranchResponse:
    with unit_of_work(db):
        branch, _ = service.update_branch(
            context,
            branch_id=branch_id,
            changes=payload.model_dump(
                mode="json", exclude_unset=True, exclude={"acknowledged_warnings"}
            ),
            if_match=if_match,
            acknowledged_warnings=payload.acknowledged_warnings,
        )
        result = _branch_response(branch, list(registrations.list_registrations()))
    _etag(response, branch.row_version)
    return result


@router.post(
    "/branches/{branch_id}/deactivate",
    response_model=BranchResponse,
    summary="Deactivate a branch",
)
def deactivate_branch(
    branch_id: uuid.UUID,
    payload: BranchStatusRequest,
    response: Response,
    context: CurrentOrg,
    db: DbSession,
    service: Annotated[CatalogueService, Depends(get_catalogue_service)],
    registrations: Annotated[
        OrganizationRegistrationRepository, Depends(get_registration_repository)
    ],
) -> BranchResponse:
    with unit_of_work(db):
        branch, _ = service.set_branch_status(
            context,
            branch_id=branch_id,
            active=False,
            reason=payload.reason,
            acknowledged_warnings=payload.acknowledged_warnings,
        )
        result = _branch_response(branch, list(registrations.list_registrations()))
    _etag(response, branch.row_version)
    return result


@router.post(
    "/branches/{branch_id}/reactivate",
    response_model=BranchResponse,
    summary="Reactivate a branch",
)
def reactivate_branch(
    branch_id: uuid.UUID,
    payload: BranchStatusRequest,
    response: Response,
    context: CurrentOrg,
    db: DbSession,
    service: Annotated[CatalogueService, Depends(get_catalogue_service)],
    registrations: Annotated[
        OrganizationRegistrationRepository, Depends(get_registration_repository)
    ],
) -> BranchResponse:
    with unit_of_work(db):
        branch, _ = service.set_branch_status(
            context,
            branch_id=branch_id,
            active=True,
            reason=payload.reason,
            acknowledged_warnings=payload.acknowledged_warnings,
        )
        result = _branch_response(branch, list(registrations.list_registrations()))
    _etag(response, branch.row_version)
    return result


# --------------------------------------------------------------------------- masters


@router.get(
    "/masters/{table_name}", response_model=list[MasterResponse], summary="List master records"
)
def list_masters(
    table_name: str,
    context: CurrentOrg,
    service: Annotated[CatalogueService, Depends(get_catalogue_service)],
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    q: Annotated[str | None, Query(min_length=2, max_length=100)] = None,
) -> list[MasterResponse]:
    rows = service.list_masters(context, table_name=table_name, status=status_filter, query=q)
    return [MasterResponse.model_validate(row) for row in rows]


@router.post(
    "/masters/{table_name}",
    response_model=MasterResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a master record",
)
def create_master(
    table_name: str,
    payload: MasterCreateRequest,
    context: CurrentOrg,
    db: DbSession,
    service: Annotated[CatalogueService, Depends(get_catalogue_service)],
) -> MasterResponse:
    values = payload.model_dump(mode="json", exclude={"acknowledged_warnings"}, exclude_none=True)
    with unit_of_work(db):
        entity, _ = service.create_master(
            context,
            table_name=table_name,
            values=values,
            acknowledged_warnings=payload.acknowledged_warnings,
        )
    return MasterResponse.model_validate(entity)


@router.get(
    "/masters/{table_name}/{entity_id}",
    response_model=MasterResponse,
    summary="Read a master record",
)
def read_master(
    table_name: str,
    entity_id: uuid.UUID,
    context: CurrentOrg,
    service: Annotated[CatalogueService, Depends(get_catalogue_service)],
) -> MasterResponse:
    entity = service.get_master(context, table_name=table_name, entity_id=entity_id)
    return MasterResponse.model_validate(entity)


@router.patch(
    "/masters/{table_name}/{entity_id}",
    response_model=MasterResponse,
    summary="Edit a master record",
)
def update_master(
    table_name: str,
    entity_id: uuid.UUID,
    payload: MasterUpdateRequest,
    response: Response,
    context: CurrentOrg,
    if_match: IfMatch,
    db: DbSession,
    service: Annotated[CatalogueService, Depends(get_catalogue_service)],
) -> MasterResponse:
    with unit_of_work(db):
        entity, _ = service.update_master(
            context,
            table_name=table_name,
            entity_id=entity_id,
            changes=payload.model_dump(
                mode="json", exclude_unset=True, exclude={"acknowledged_warnings"}
            ),
            if_match=if_match,
            acknowledged_warnings=payload.acknowledged_warnings,
        )
    _etag(response, entity.row_version)
    return MasterResponse.model_validate(entity)


@router.post(
    "/masters/{table_name}/{entity_id}/deactivate",
    response_model=MasterResponse,
    summary="Deactivate a master record",
)
def deactivate_master(
    table_name: str,
    entity_id: uuid.UUID,
    payload: MasterStatusRequest,
    response: Response,
    context: CurrentOrg,
    db: DbSession,
    service: Annotated[CatalogueService, Depends(get_catalogue_service)],
) -> MasterResponse:
    with unit_of_work(db):
        entity, _ = service.set_master_status(
            context,
            table_name=table_name,
            entity_id=entity_id,
            active=False,
            reason=payload.reason,
            acknowledged_warnings=payload.acknowledged_warnings,
        )
    _etag(response, entity.row_version)
    return MasterResponse.model_validate(entity)


@router.post(
    "/masters/{table_name}/{entity_id}/reactivate",
    response_model=MasterResponse,
    summary="Reactivate a master record",
)
def reactivate_master(
    table_name: str,
    entity_id: uuid.UUID,
    payload: MasterStatusRequest,
    response: Response,
    context: CurrentOrg,
    db: DbSession,
    service: Annotated[CatalogueService, Depends(get_catalogue_service)],
) -> MasterResponse:
    with unit_of_work(db):
        entity, _ = service.set_master_status(
            context,
            table_name=table_name,
            entity_id=entity_id,
            active=True,
            reason=payload.reason,
            acknowledged_warnings=payload.acknowledged_warnings,
        )
    _etag(response, entity.row_version)
    return MasterResponse.model_validate(entity)


# --------------------------------------------------------------------------- lookups


@router.get(
    "/lookups/{list_code}", response_model=list[LookupResponse], summary="List lookup values"
)
def list_lookups(
    list_code: str,
    context: CurrentOrg,
    service: Annotated[CatalogueService, Depends(get_catalogue_service)],
    status_filter: Annotated[str | None, Query(alias="status")] = None,
) -> list[LookupResponse]:
    rows = service.list_lookups(context, list_code=list_code, status=status_filter)
    return [LookupResponse.model_validate(row) for row in rows]


@router.post(
    "/lookups/{list_code}",
    response_model=LookupResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a lookup value",
)
def create_lookup(
    list_code: str,
    payload: LookupCreateRequest,
    context: CurrentOrg,
    db: DbSession,
    service: Annotated[CatalogueService, Depends(get_catalogue_service)],
) -> LookupResponse:
    values = payload.model_dump(mode="json")
    values["list_code"] = list_code
    with unit_of_work(db):
        lookup = service.create_lookup(context, values=values)
    return LookupResponse.model_validate(lookup)


@router.get(
    "/lookups/{list_code}/{value_code}",
    response_model=LookupResponse,
    summary="Read a lookup value",
)
def read_lookup(
    list_code: str,
    value_code: str,
    response: Response,
    context: CurrentOrg,
    service: Annotated[CatalogueService, Depends(get_catalogue_service)],
) -> LookupResponse:
    lookup = service.get_lookup(context, list_code=list_code, value_code=value_code)
    _etag(response, lookup.row_version)
    return LookupResponse.model_validate(lookup)


@router.patch(
    "/lookups/{list_code}/{value_code}",
    response_model=LookupResponse,
    summary="Edit a lookup value",
)
def update_lookup(
    list_code: str,
    value_code: str,
    payload: LookupUpdateRequest,
    response: Response,
    context: CurrentOrg,
    if_match: IfMatch,
    db: DbSession,
    service: Annotated[CatalogueService, Depends(get_catalogue_service)],
) -> LookupResponse:
    with unit_of_work(db):
        lookup = service.update_lookup(
            context,
            list_code=list_code,
            value_code=value_code,
            changes=payload.model_dump(mode="json", exclude_unset=True),
            if_match=if_match,
        )
    _etag(response, lookup.row_version)
    return LookupResponse.model_validate(lookup)


# --------------------------------------------------------------------------- custom statuses


@router.get(
    "/custom-statuses",
    response_model=list[CustomStatusResponse],
    summary="List custom employee statuses",
)
def list_custom_statuses(
    context: CurrentOrg,
    service: Annotated[CatalogueService, Depends(get_catalogue_service)],
) -> list[CustomStatusResponse]:
    rows = service.list_custom_statuses(context)
    return [CustomStatusResponse.model_validate(row) for row in rows]


@router.post(
    "/custom-statuses",
    response_model=CustomStatusResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a custom employee status",
)
def create_custom_status(
    payload: CustomStatusCreateRequest,
    context: CurrentOrg,
    db: DbSession,
    service: Annotated[CatalogueService, Depends(get_catalogue_service)],
) -> CustomStatusResponse:
    with unit_of_work(db):
        entity = service.create_custom_status(context, values=payload.model_dump(mode="json"))
    return CustomStatusResponse.model_validate(entity)


@router.patch(
    "/custom-statuses/{code}",
    response_model=CustomStatusResponse,
    summary="Edit a custom employee status",
)
def update_custom_status(
    code: str,
    payload: CustomStatusUpdateRequest,
    response: Response,
    context: CurrentOrg,
    if_match: IfMatch,
    db: DbSession,
    service: Annotated[CatalogueService, Depends(get_catalogue_service)],
) -> CustomStatusResponse:
    with unit_of_work(db):
        entity = service.update_custom_status(
            context,
            code=code,
            changes=payload.model_dump(mode="json", exclude_unset=True),
            if_match=if_match,
        )
    _etag(response, entity.row_version)
    return CustomStatusResponse.model_validate(entity)


__all__ = ["router"]
