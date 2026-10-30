"""Positions: CRUD, lifecycle, occupancy, history and competencies (JA §13.6).

There is deliberately no ``POST /positions/{id}/assign`` here. JA §13.7 routes occupancy
through the existing employee job-change endpoint, so the employee record, its job history and
the position's occupancy are written in one transaction and there is only one write path to
get wrong.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, Response, status

from app.api.deps import (
    CurrentOrg,
    DbSession,
    IdempotencyKeyHeader,
    IfMatch,
    get_competency_service,
    get_cursor_codec,
    get_idempotency_repository,
    get_job_catalogue_service,
    get_position_service,
)
from app.api.idempotency import idempotency_guard, require_idempotency_key
from app.core.errors import ValidationFailed
from app.db.session import unit_of_work
from app.models.position import Position
from app.repositories.platform import IdempotencyRepository
from app.schemas.common import (
    CursorCodec,
    Page,
    ReasonRequest,
    SortDirection,
    SortSpec,
    VersionedPayload,
    etag_for,
    next_cursor_for,
)
from app.schemas.position import (
    CompetencyMappingReplaceRequest,
    CompetencyRequirementResponse,
    GradeSummary,
    PositionCreateRequest,
    PositionHeadcountResponse,
    PositionHistoryItem,
    PositionLifecycleRequest,
    PositionOccupancy,
    PositionResponse,
    PositionSummary,
    PositionUpdateRequest,
)
from app.services.capacity import CapacitySnapshot
from app.services.competencies import CompetencyService
from app.services.job_catalogue import JobCatalogueService
from app.services.positions import POSITION_SORT_FIELDS, PositionService

router = APIRouter(prefix="/orgs/{org_id}/positions", tags=["Positions"])


def _etag(response: Response, row_version: int) -> None:
    response.headers["ETag"] = etag_for(row_version)


def _finalise[ModelT: VersionedPayload](response: Response, row: Any, result: ModelT) -> ModelT:
    """Publish the committed ``row_version`` as both the ETag and the body's field.

    The body is built inside the transaction, so its ``row_version`` is one behind until the
    flush that the commit performs. Reading it from the row *after* the commit keeps the
    header and the payload in step, so a client can take either and use it as ``If-Match``.
    """
    response.headers["ETag"] = etag_for(row.row_version)
    result.row_version = row.row_version
    return result


def _occupancy(snapshot: CapacitySnapshot) -> PositionOccupancy:
    return PositionOccupancy(
        as_of=snapshot.as_of,
        authorized_fte=snapshot.authorized_fte,
        occupied_fte=snapshot.occupied_fte,
        reserved_fte=snapshot.reserved_fte,
        vacant_fte=snapshot.vacant_fte,
        max_incumbents=snapshot.max_incumbents,
        occupied_headcount=snapshot.occupied_headcount,
        reserved_headcount=snapshot.reserved_headcount,
        vacant_headcount=snapshot.vacant_headcount,
        is_fillable=snapshot.is_fillable,
    )


def _summary(
    position: Position, snapshot: CapacitySnapshot, job_titles: dict[uuid.UUID, str]
) -> PositionSummary:
    return PositionSummary(
        id=position.id,
        position_code=position.position_code,
        position_title=position.position_title,
        job_id=position.job_id,
        job_title=job_titles.get(position.job_id),
        department_id=position.department_id,
        branch_id=position.branch_id,
        grade_id=position.grade_id,
        position_status=position.position_status,
        authorized_fte=snapshot.authorized_fte,
        occupied_fte=snapshot.occupied_fte,
        vacant_fte=snapshot.vacant_fte,
        max_incumbents=snapshot.max_incumbents,
        occupied_headcount=snapshot.occupied_headcount,
        vacant_headcount=snapshot.vacant_headcount,
        row_version=position.row_version,
    )


def _response(
    position: Position,
    snapshot: CapacitySnapshot,
    *,
    catalogue: JobCatalogueService,
    competencies: CompetencyService,
    context: CurrentOrg,
    include_competencies: bool = True,
) -> PositionResponse:
    jobs = catalogue.jobs_for([position.job_id])
    grades = catalogue.grades_for([position.grade_id])
    job = jobs.get(position.job_id)
    grade = grades.get(position.grade_id) if position.grade_id else None
    requirements: list[CompetencyRequirementResponse] = []
    if include_competencies:
        rows = competencies.position_requirements(context, position_id=position.id)
        labels = competencies.competency_labels([row.competency_id for row in rows])
        requirements = [
            CompetencyRequirementResponse(
                id=row.mapping_id,
                competency_id=row.competency_id,
                competency_code=labels[row.competency_id].competency_code
                if row.competency_id in labels
                else None,
                competency_name=labels[row.competency_id].name
                if row.competency_id in labels
                else None,
                requirement_type=row.requirement_type,
                min_proficiency=row.min_proficiency,
                display_order=row.display_order,
                effective_from=row.effective_from,
                effective_to=row.effective_to,
                row_version=row.row_version,
                source=row.source,
            )
            for row in rows
        ]
    return PositionResponse(
        id=position.id,
        organization_id=position.organization_id,
        position_code=position.position_code,
        position_title=position.position_title,
        job_id=position.job_id,
        department_id=position.department_id,
        sub_department_id=position.sub_department_id,
        branch_id=position.branch_id,
        work_location_id=position.work_location_id,
        work_mode=position.work_mode,
        business_unit_id=position.business_unit_id,
        cost_center_id=position.cost_center_id,
        grade_id=position.grade_id,
        reports_to_position_id=position.reports_to_position_id,
        authorized_fte=position.authorized_fte,
        max_incumbents=position.max_incumbents,
        position_status=position.position_status,
        planned_start_date=position.planned_start_date,
        planned_end_date=position.planned_end_date,
        opened_at=position.opened_at,
        closed_at=position.closed_at,
        freeze_reason=position.freeze_reason,
        close_reason=position.close_reason,
        row_version=position.row_version,
        archived_at=position.archived_at,
        job=catalogue.job_summary(job) if job else None,
        grade=GradeSummary(id=grade.id, code=grade.code, name=grade.name, rank=grade.rank)
        if grade
        else None,
        occupancy=_occupancy(snapshot),
        competencies=requirements,
    )


# ======================================================================== reads


@router.get("", response_model=Page[PositionSummary], summary="List and search positions")
def list_positions(
    context: CurrentOrg,
    service: Annotated[PositionService, Depends(get_position_service)],
    catalogue: Annotated[JobCatalogueService, Depends(get_job_catalogue_service)],
    codec: Annotated[CursorCodec, Depends(get_cursor_codec)],
    q: Annotated[str | None, Query(min_length=2, max_length=100)] = None,
    position_status: Annotated[list[str] | None, Query()] = None,
    job_id: uuid.UUID | None = None,
    department_id: uuid.UUID | None = None,
    sub_department_id: uuid.UUID | None = None,
    branch_id: uuid.UUID | None = None,
    grade_id: uuid.UUID | None = None,
    reports_to_position_id: uuid.UUID | None = None,
    include_indirect: bool = False,
    has_vacancy: bool | None = None,
    as_of: date | None = None,
    include_archived: bool = False,
    sort: str | None = None,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    with_total: bool = False,
) -> Page[PositionSummary]:
    """JA §17.4. List items carry counts and labels, never employee personal data."""
    spec = SortSpec.parse(sort, allowed=POSITION_SORT_FIELDS, default_field="position_code")
    after_value, after_id, direction = _decode_cursor(codec, cursor, spec)
    items, has_more, total = service.list_positions(
        context,
        query=q,
        position_status=position_status,
        job_id=job_id,
        department_id=department_id,
        sub_department_id=sub_department_id,
        branch_id=branch_id,
        grade_id=grade_id,
        reports_to_position_id=reports_to_position_id,
        include_indirect=include_indirect,
        has_vacancy=has_vacancy,
        as_of=as_of,
        include_archived=include_archived,
        sort_field=spec.field,
        direction=direction.value,
        after_value=after_value,
        after_id=after_id,
        limit=limit,
        with_total=with_total,
    )
    job_titles = {
        job_id_: job.title
        for job_id_, job in catalogue.jobs_for([position.job_id for position, _ in items]).items()
    }
    return Page[PositionSummary](
        items=[_summary(position, snapshot, job_titles) for position, snapshot in items],
        next_cursor=next_cursor_for(
            codec,
            rows=[position for position, _ in items],
            has_more=has_more,
            sort_field=spec.field,
            direction=direction,
            value_of=lambda row: getattr(row, _sort_column(spec.field)),
            id_of=lambda row: row.id,
        ),
        total_count=total,
    )


@router.post(
    "",
    response_model=PositionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a DRAFT position",
)
def create_position(
    payload: PositionCreateRequest,
    request: Request,
    response: Response,
    context: CurrentOrg,
    db: DbSession,
    idempotency_key: IdempotencyKeyHeader,
    service: Annotated[PositionService, Depends(get_position_service)],
    catalogue: Annotated[JobCatalogueService, Depends(get_job_catalogue_service)],
    competencies: Annotated[CompetencyService, Depends(get_competency_service)],
    idempotency: Annotated[IdempotencyRepository, Depends(get_idempotency_repository)],
) -> PositionResponse:
    """JA §13.6. ``Idempotency-Key`` is required: a retry must not authorize a second seat."""
    key = require_idempotency_key(idempotency_key, endpoint="Creating a position")
    body = payload.model_dump(mode="json")
    guard = idempotency_guard(
        repository=idempotency,
        context=context,
        key=key,
        method="POST",
        path=str(request.url.path),
        payload=body,
    )
    replayed = guard.replay()
    if replayed is not None:
        return PositionResponse.model_validate(replayed)

    values = payload.model_dump(exclude={"acknowledged_warnings", "competencies"})
    with unit_of_work(db):
        guard.claim()
        position, _ = service.create_position(
            context, values=values, acknowledged_warnings=payload.acknowledged_warnings
        )
        if payload.competencies:
            competencies.seed_initial_mappings(
                context,
                owner_field="position_id",
                owner_id=position.id,
                mappings=[item.model_dump() for item in payload.competencies],
            )
        _, snapshot = service.get_position(context, position.id)
        result = _response(
            position,
            snapshot,
            catalogue=catalogue,
            competencies=competencies,
            context=context,
        )
        guard.complete(status_code=status.HTTP_201_CREATED, body=result.model_dump(mode="json"))
    return _finalise(response, position, result)


@router.get("/{position_id}", response_model=PositionResponse, summary="Read a position")
def read_position(
    position_id: uuid.UUID,
    response: Response,
    context: CurrentOrg,
    service: Annotated[PositionService, Depends(get_position_service)],
    catalogue: Annotated[JobCatalogueService, Depends(get_job_catalogue_service)],
    competencies: Annotated[CompetencyService, Depends(get_competency_service)],
    as_of: date | None = None,
) -> PositionResponse:
    position, snapshot = service.get_position(context, position_id, as_of=as_of)
    _etag(response, position.row_version)
    return _response(
        position, snapshot, catalogue=catalogue, competencies=competencies, context=context
    )


@router.get(
    "/{position_id}/headcount",
    response_model=PositionHeadcountResponse,
    summary="Read one position's occupancy and vacancy",
)
def read_position_headcount(
    position_id: uuid.UUID,
    context: CurrentOrg,
    service: Annotated[PositionService, Depends(get_position_service)],
    as_of: date | None = None,
) -> PositionHeadcountResponse:
    """JA §13.6: counts for one position.

    Available to ``position.view`` because a caller who may fill a position must be able to
    see whether it has room. Organization-wide totals need ``headcount.view`` instead.
    """
    position, snapshot = service.occupancy(context, position_id, as_of=as_of)
    return PositionHeadcountResponse(
        position_id=position.id,
        position_code=position.position_code,
        position_status=position.position_status,
        occupancy=_occupancy(snapshot),
    )


@router.get(
    "/{position_id}/history",
    response_model=Page[PositionHistoryItem],
    summary="Read a position's status, headcount and assignment history",
)
def read_position_history(
    position_id: uuid.UUID,
    context: CurrentOrg,
    service: Annotated[PositionService, Depends(get_position_service)],
    from_: Annotated[datetime | None, Query(alias="from")] = None,
    to: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
) -> Page[PositionHistoryItem]:
    rows = service.history(context, position_id, since=from_, until=to)
    return Page[PositionHistoryItem](items=[PositionHistoryItem(**row) for row in rows[:limit]])


@router.get(
    "/{position_id}/competencies",
    response_model=list[CompetencyRequirementResponse],
    summary="Read a position's effective competency requirements",
)
def read_position_competencies(
    position_id: uuid.UUID,
    context: CurrentOrg,
    service: Annotated[CompetencyService, Depends(get_competency_service)],
    as_of: date | None = None,
) -> list[CompetencyRequirementResponse]:
    """The JA §9.3 union of the job's baseline and the position's additions."""
    rows = service.position_requirements(context, position_id=position_id, as_of=as_of)
    labels = service.competency_labels([row.competency_id for row in rows])
    return [
        CompetencyRequirementResponse(
            id=row.mapping_id,
            competency_id=row.competency_id,
            competency_code=labels[row.competency_id].competency_code
            if row.competency_id in labels
            else None,
            competency_name=labels[row.competency_id].name if row.competency_id in labels else None,
            requirement_type=row.requirement_type,
            min_proficiency=row.min_proficiency,
            display_order=row.display_order,
            effective_from=row.effective_from,
            effective_to=row.effective_to,
            row_version=row.row_version,
            source=row.source,
        )
        for row in rows
    ]


# ======================================================================= writes


@router.patch("/{position_id}", response_model=PositionResponse, summary="Edit a position")
def update_position(
    position_id: uuid.UUID,
    payload: PositionUpdateRequest,
    response: Response,
    context: CurrentOrg,
    if_match: IfMatch,
    db: DbSession,
    service: Annotated[PositionService, Depends(get_position_service)],
    catalogue: Annotated[JobCatalogueService, Depends(get_job_catalogue_service)],
    competencies: Annotated[CompetencyService, Depends(get_competency_service)],
) -> PositionResponse:
    """Definition only. Capacity changes go through a headcount change request (JA §8.7)."""
    changes = payload.model_dump(exclude_unset=True, exclude={"acknowledged_warnings"})
    with unit_of_work(db):
        position, _ = service.update_position(
            context,
            position_id=position_id,
            changes=changes,
            if_match=if_match,
            acknowledged_warnings=payload.acknowledged_warnings,
        )
        _, snapshot = service.get_position(context, position.id)
        result = _response(
            position,
            snapshot,
            catalogue=catalogue,
            competencies=competencies,
            context=context,
        )
    return _finalise(response, position, result)


@router.put(
    "/{position_id}/competencies",
    response_model=list[CompetencyRequirementResponse],
    summary="Replace a position's additional competency requirements",
)
def replace_position_competencies(
    position_id: uuid.UUID,
    payload: CompetencyMappingReplaceRequest,
    context: CurrentOrg,
    if_match: IfMatch,
    db: DbSession,
    service: Annotated[CompetencyService, Depends(get_competency_service)],
) -> list[CompetencyRequirementResponse]:
    with unit_of_work(db):
        rows = service.replace_position_requirements(
            context,
            position_id=position_id,
            mappings=[item.model_dump() for item in payload.mappings],
            reason=payload.reason,
            if_match=if_match,
            acknowledged_warnings=payload.acknowledged_warnings,
        )
        labels = service.competency_labels([row.competency_id for row in rows])
        return [
            CompetencyRequirementResponse(
                id=row.mapping_id,
                competency_id=row.competency_id,
                competency_code=labels[row.competency_id].competency_code
                if row.competency_id in labels
                else None,
                competency_name=labels[row.competency_id].name
                if row.competency_id in labels
                else None,
                requirement_type=row.requirement_type,
                min_proficiency=row.min_proficiency,
                display_order=row.display_order,
                effective_from=row.effective_from,
                effective_to=row.effective_to,
                row_version=row.row_version,
                source=row.source,
            )
            for row in rows
        ]


def _lifecycle_response(
    position: Position,
    service: PositionService,
    catalogue: JobCatalogueService,
    competencies: CompetencyService,
    context: CurrentOrg,
) -> PositionResponse:
    _, snapshot = service.get_position(context, position.id)
    return _response(
        position, snapshot, catalogue=catalogue, competencies=competencies, context=context
    )


@router.post(
    "/{position_id}/open",
    response_model=PositionResponse,
    summary="Authorize a position for filling",
)
def open_position(
    position_id: uuid.UUID,
    payload: PositionLifecycleRequest,
    response: Response,
    context: CurrentOrg,
    if_match: IfMatch,
    db: DbSession,
    service: Annotated[PositionService, Depends(get_position_service)],
    catalogue: Annotated[JobCatalogueService, Depends(get_job_catalogue_service)],
    competencies: Annotated[CompetencyService, Depends(get_competency_service)],
) -> PositionResponse:
    with unit_of_work(db):
        position = service.open_position(
            context,
            position_id=position_id,
            reason=payload.reason,
            headcount_change_request_id=payload.headcount_change_request_id,
            if_match=if_match,
        )
        result = _lifecycle_response(position, service, catalogue, competencies, context)
    return _finalise(response, position, result)


@router.post("/{position_id}/freeze", response_model=PositionResponse, summary="Stop new fills")
def freeze_position(
    position_id: uuid.UUID,
    payload: PositionLifecycleRequest,
    response: Response,
    context: CurrentOrg,
    if_match: IfMatch,
    db: DbSession,
    service: Annotated[PositionService, Depends(get_position_service)],
    catalogue: Annotated[JobCatalogueService, Depends(get_job_catalogue_service)],
    competencies: Annotated[CompetencyService, Depends(get_competency_service)],
) -> PositionResponse:
    reason = _require_reason(payload, "freeze")
    with unit_of_work(db):
        position = service.freeze_position(
            context, position_id=position_id, reason=reason, if_match=if_match
        )
        result = _lifecycle_response(position, service, catalogue, competencies, context)
    return _finalise(response, position, result)


@router.post(
    "/{position_id}/reopen",
    response_model=PositionResponse,
    summary="Resume filling a frozen position",
)
def reopen_position(
    position_id: uuid.UUID,
    payload: PositionLifecycleRequest,
    response: Response,
    context: CurrentOrg,
    if_match: IfMatch,
    db: DbSession,
    service: Annotated[PositionService, Depends(get_position_service)],
    catalogue: Annotated[JobCatalogueService, Depends(get_job_catalogue_service)],
    competencies: Annotated[CompetencyService, Depends(get_competency_service)],
) -> PositionResponse:
    """``FROZEN -> OPEN``. Reopening a *closed* position is a new authorization and needs an
    approved headcount change request (JA §15.2), which ``open`` handles."""
    with unit_of_work(db):
        position = service.open_position(
            context,
            position_id=position_id,
            reason=payload.reason,
            headcount_change_request_id=payload.headcount_change_request_id,
            if_match=if_match,
        )
        result = _lifecycle_response(position, service, catalogue, competencies, context)
    return _finalise(response, position, result)


@router.post(
    "/{position_id}/close",
    response_model=PositionResponse,
    summary="Close a position with zero occupancy",
)
def close_position(
    position_id: uuid.UUID,
    payload: PositionLifecycleRequest,
    response: Response,
    context: CurrentOrg,
    if_match: IfMatch,
    db: DbSession,
    service: Annotated[PositionService, Depends(get_position_service)],
    catalogue: Annotated[JobCatalogueService, Depends(get_job_catalogue_service)],
    competencies: Annotated[CompetencyService, Depends(get_competency_service)],
) -> PositionResponse:
    reason = _require_reason(payload, "close")
    with unit_of_work(db):
        position = service.close_position(
            context, position_id=position_id, reason=reason, if_match=if_match
        )
        result = _lifecycle_response(position, service, catalogue, competencies, context)
    return _finalise(response, position, result)


@router.post(
    "/{position_id}/archive", response_model=PositionResponse, summary="Archive a position"
)
def archive_position(
    position_id: uuid.UUID,
    payload: ReasonRequest,
    response: Response,
    context: CurrentOrg,
    if_match: IfMatch,
    db: DbSession,
    service: Annotated[PositionService, Depends(get_position_service)],
    catalogue: Annotated[JobCatalogueService, Depends(get_job_catalogue_service)],
    competencies: Annotated[CompetencyService, Depends(get_competency_service)],
) -> PositionResponse:
    with unit_of_work(db):
        position = service.archive_position(
            context, position_id=position_id, reason=payload.reason, if_match=if_match
        )
        result = _lifecycle_response(position, service, catalogue, competencies, context)
    return _finalise(response, position, result)


# ------------------------------------------------------------------- plumbing


def _require_reason(payload: PositionLifecycleRequest, action: str) -> str:
    """JA §15.2: freeze and close must say why. One schema serves four actions."""
    if payload.reason is None:
        raise ValidationFailed(f"A reason is required to {action} a position.").with_detail(
            field="reason", code="REQUIRED", message="Ten characters or more."
        )
    return payload.reason


def _decode_cursor(
    codec: CursorCodec, cursor: str | None, spec: SortSpec
) -> tuple[Any, uuid.UUID | None, SortDirection]:
    if cursor is None:
        return None, None, spec.direction
    payload = codec.decode(cursor)
    if payload.sort_field != spec.field:
        raise ValidationFailed("The cursor was issued for a different sort.")
    return payload.sort_value, payload.last_id, payload.direction


def _sort_column(field: str) -> str:
    return {
        "position_code": "position_code_normalized",
        "position_title": "position_title",
        "position_status": "position_status",
        "authorized_fte": "authorized_fte",
        "created_at": "created_at",
    }[field]


__all__ = ["router"]
