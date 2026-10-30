"""Education, previous employment, family and nominations (§24.4.4 remainder)."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Response, status

from app.api.deps import CurrentOrg, DbSession, get_profile_record_service
from app.core.context import OrgContext
from app.core.permissions import Permission
from app.db.session import unit_of_work
from app.models.profile import EmployeePreviousEmployment
from app.schemas.common import ReasonRequest, etag_for
from app.schemas.profile import (
    EducationCreateRequest,
    EducationResponse,
    EducationUpdateRequest,
    FamilyCreateRequest,
    FamilyResponse,
    FamilyUpdateRequest,
    NominationReplaceRequest,
    NominationSetResponse,
    NomineeResponse,
    OverlapReviewRequest,
    PreviousEmploymentCreateRequest,
    PreviousEmploymentResponse,
    PreviousEmploymentUpdateRequest,
)
from app.services.profile_records import ProfileRecordService

router = APIRouter(prefix="/orgs/{org_id}/employees/{employee_id}", tags=["Profile records"])


def _previous_response(
    context: OrgContext, row: EmployeePreviousEmployment, payslips: list[uuid.UUID]
) -> PreviousEmploymentResponse:
    payload = PreviousEmploymentResponse.model_validate(row)
    payload.payslip_document_ids = payslips
    if not context.has(Permission.SALARY_VIEW):
        payload.last_drawn_ctc = None
        payload.last_drawn_ctc_currency = None
    if not context.has(Permission.BGV_VIEW):
        payload.background_verification_status = None
        payload.background_verification_date = None
        payload.reference_name = None
        payload.reference_contact = None
    return payload


@router.get("/education", response_model=list[EducationResponse], summary="List education")
def list_education(
    employee_id: uuid.UUID,
    context: CurrentOrg,
    service: Annotated[ProfileRecordService, Depends(get_profile_record_service)],
) -> list[EducationResponse]:
    return [
        EducationResponse.model_validate(row)
        for row in service.list_education(context, employee_id)
    ]


@router.post(
    "/education",
    response_model=EducationResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Add education",
)
def create_education(
    employee_id: uuid.UUID,
    payload: EducationCreateRequest,
    context: CurrentOrg,
    db: DbSession,
    service: Annotated[ProfileRecordService, Depends(get_profile_record_service)],
) -> EducationResponse:
    with unit_of_work(db):
        row = service.create_education(context, employee_id, payload.model_dump())
    return EducationResponse.model_validate(row)


@router.patch("/education/{record_id}", response_model=EducationResponse, summary="Edit education")
def update_education(
    employee_id: uuid.UUID,
    record_id: uuid.UUID,
    payload: EducationUpdateRequest,
    response: Response,
    context: CurrentOrg,
    db: DbSession,
    service: Annotated[ProfileRecordService, Depends(get_profile_record_service)],
) -> EducationResponse:
    with unit_of_work(db):
        row = service.update_education(
            context, employee_id, record_id, payload.model_dump(exclude_unset=True)
        )
        result = EducationResponse.model_validate(row)
    response.headers["ETag"] = etag_for(row.row_version)
    return result


@router.post("/education/{record_id}/archive", status_code=status.HTTP_204_NO_CONTENT)
def archive_education(
    employee_id: uuid.UUID,
    record_id: uuid.UUID,
    payload: ReasonRequest,
    context: CurrentOrg,
    db: DbSession,
    service: Annotated[ProfileRecordService, Depends(get_profile_record_service)],
) -> Response:
    with unit_of_work(db):
        service.archive_education(context, employee_id, record_id, payload.reason)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/previous-employment",
    response_model=list[PreviousEmploymentResponse],
    summary="List previous employment",
)
def list_previous_employment(
    employee_id: uuid.UUID,
    context: CurrentOrg,
    service: Annotated[ProfileRecordService, Depends(get_profile_record_service)],
) -> list[PreviousEmploymentResponse]:
    return [
        _previous_response(context, row, payslips)
        for row, payslips in service.list_previous_employment(context, employee_id)
    ]


@router.post(
    "/previous-employment",
    response_model=PreviousEmploymentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Add previous employment",
)
def create_previous_employment(
    employee_id: uuid.UUID,
    payload: PreviousEmploymentCreateRequest,
    context: CurrentOrg,
    db: DbSession,
    service: Annotated[ProfileRecordService, Depends(get_profile_record_service)],
) -> PreviousEmploymentResponse:
    with unit_of_work(db):
        row, payslips = service.create_previous_employment(
            context, employee_id, payload.model_dump()
        )
        return _previous_response(context, row, payslips)


@router.patch(
    "/previous-employment/{record_id}",
    response_model=PreviousEmploymentResponse,
    summary="Edit previous employment",
)
def update_previous_employment(
    employee_id: uuid.UUID,
    record_id: uuid.UUID,
    payload: PreviousEmploymentUpdateRequest,
    response: Response,
    context: CurrentOrg,
    db: DbSession,
    service: Annotated[ProfileRecordService, Depends(get_profile_record_service)],
) -> PreviousEmploymentResponse:
    with unit_of_work(db):
        row, payslips = service.update_previous_employment(
            context, employee_id, record_id, payload.model_dump(exclude_unset=True)
        )
        result = _previous_response(context, row, payslips)
    response.headers["ETag"] = etag_for(row.row_version)
    return result


@router.post("/previous-employment/{record_id}/archive", status_code=status.HTTP_204_NO_CONTENT)
def archive_previous_employment(
    employee_id: uuid.UUID,
    record_id: uuid.UUID,
    payload: ReasonRequest,
    context: CurrentOrg,
    db: DbSession,
    service: Annotated[ProfileRecordService, Depends(get_profile_record_service)],
) -> Response:
    with unit_of_work(db):
        service.archive_previous_employment(context, employee_id, record_id, payload.reason)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/previous-employment/{record_id}/overlap-review",
    response_model=PreviousEmploymentResponse,
    summary="Accept a flagged employment overlap",
)
def review_overlap(
    employee_id: uuid.UUID,
    record_id: uuid.UUID,
    payload: OverlapReviewRequest,
    context: CurrentOrg,
    db: DbSession,
    service: Annotated[ProfileRecordService, Depends(get_profile_record_service)],
) -> PreviousEmploymentResponse:
    with unit_of_work(db):
        row = service.review_overlap(context, employee_id, record_id, remarks=payload.remarks)
        payslips = [
            link.document_id for link in service._previous_documents.list_for_employment(row.id)
        ]
        return _previous_response(context, row, payslips)


@router.get("/family-members", response_model=list[FamilyResponse], summary="List family")
def list_family(
    employee_id: uuid.UUID,
    context: CurrentOrg,
    service: Annotated[ProfileRecordService, Depends(get_profile_record_service)],
) -> list[FamilyResponse]:
    return [FamilyResponse.model_validate(row) for row in service.list_family(context, employee_id)]


@router.post(
    "/family-members",
    response_model=FamilyResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Add a family member",
)
def create_family(
    employee_id: uuid.UUID,
    payload: FamilyCreateRequest,
    context: CurrentOrg,
    db: DbSession,
    service: Annotated[ProfileRecordService, Depends(get_profile_record_service)],
) -> FamilyResponse:
    with unit_of_work(db):
        row = service.create_family(context, employee_id, payload.model_dump())
    return FamilyResponse.model_validate(row)


@router.patch(
    "/family-members/{record_id}", response_model=FamilyResponse, summary="Edit a family member"
)
def update_family(
    employee_id: uuid.UUID,
    record_id: uuid.UUID,
    payload: FamilyUpdateRequest,
    response: Response,
    context: CurrentOrg,
    db: DbSession,
    service: Annotated[ProfileRecordService, Depends(get_profile_record_service)],
) -> FamilyResponse:
    with unit_of_work(db):
        row = service.update_family(
            context, employee_id, record_id, payload.model_dump(exclude_unset=True)
        )
        result = FamilyResponse.model_validate(row)
    response.headers["ETag"] = etag_for(row.row_version)
    return result


@router.post("/family-members/{record_id}/archive", status_code=status.HTTP_204_NO_CONTENT)
def archive_family(
    employee_id: uuid.UUID,
    record_id: uuid.UUID,
    payload: ReasonRequest,
    context: CurrentOrg,
    db: DbSession,
    service: Annotated[ProfileRecordService, Depends(get_profile_record_service)],
) -> Response:
    with unit_of_work(db):
        service.archive_family(context, employee_id, record_id, payload.reason)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/nominations",
    response_model=list[NominationSetResponse],
    summary="Read nomination sets",
)
def list_nominations(
    employee_id: uuid.UUID,
    context: CurrentOrg,
    service: Annotated[ProfileRecordService, Depends(get_profile_record_service)],
    nomination_type: str | None = None,
    as_of: date | None = None,
) -> list[NominationSetResponse]:
    sets = service.nomination_sets(
        context, employee_id, nomination_type=nomination_type, as_of=as_of
    )
    return [
        NominationSetResponse(
            nomination_type=item["nomination_type"],
            effective_from=item["effective_from"],
            effective_to=item["effective_to"],
            nomination_label=item["nomination_label"],
            nominees=[NomineeResponse.model_validate(row) for row in item["nominees"]],
            total_share=item["total_share"],
        )
        for item in sets
    ]


@router.put(
    "/nominations/{nomination_type}",
    response_model=NominationSetResponse,
    summary="Replace a nomination set atomically",
)
def replace_nominations(
    employee_id: uuid.UUID,
    nomination_type: str,
    payload: NominationReplaceRequest,
    context: CurrentOrg,
    db: DbSession,
    service: Annotated[ProfileRecordService, Depends(get_profile_record_service)],
) -> NominationSetResponse:
    with unit_of_work(db):
        rows = service.replace_nominations(
            context,
            employee_id,
            nomination_type,
            effective_from=payload.effective_from,
            nomination_label=payload.nomination_label,
            nominees=payload.nominees,
        )
        return NominationSetResponse(
            nomination_type=nomination_type,
            effective_from=payload.effective_from,
            effective_to=None,
            nomination_label=payload.nomination_label,
            nominees=[NomineeResponse.model_validate(row) for row in rows],
            total_share=sum(row.share_percentage for row in rows),
        )


__all__ = ["router"]
