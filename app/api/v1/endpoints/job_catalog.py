"""Job families, the job catalogue and competencies (JA §13.2, §13.3, §13.4).

Paths are ``job-families``, ``job-catalog`` and ``competencies``, and none of them is added to
the generic ``/masters/{type}`` registry — JA §13.12 is explicit about both: the generic route
would shadow the dedicated logic and expose only the generic master shape, and ``/jobs`` would
be ambiguous with ``/employees/{id}/job-changes``.

Handlers here parse, delegate and serialise. Every business rule lives in
:class:`~app.services.job_catalogue.JobCatalogueService` or
:class:`~app.services.competencies.CompetencyService`.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Response, status

from app.api.deps import (
    CurrentOrg,
    DbSession,
    IfMatch,
    get_competency_service,
    get_cursor_codec,
    get_job_catalogue_service,
)
from app.core.errors import ValidationFailed
from app.db.session import unit_of_work
from app.models.position import Competency, Job, JobFamily
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
    CompetencyCreateRequest,
    CompetencyMappingReplaceRequest,
    CompetencyRequirementResponse,
    CompetencyResponse,
    CompetencyUpdateRequest,
    GradeSummary,
    JobCreateRequest,
    JobFamilyCreateRequest,
    JobFamilyResponse,
    JobFamilySummary,
    JobFamilyUpdateRequest,
    JobResponse,
    JobSummary,
    JobUpdateRequest,
)
from app.services.competencies import (
    COMPETENCY_SORT_FIELDS,
    CompetencyService,
    EffectiveRequirement,
)
from app.services.job_catalogue import (
    FAMILY_SORT_FIELDS,
    JOB_SORT_FIELDS,
    JobCatalogueService,
)

router = APIRouter(prefix="/orgs/{org_id}", tags=["Job architecture"])


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


# --------------------------------------------------------------------- serialisers


def _family_summary(family: JobFamily | None) -> JobFamilySummary | None:
    if family is None:
        return None
    return JobFamilySummary(
        id=family.id,
        family_code=family.family_code,
        name=family.name,
        node_level=family.node_level,
    )


def _family_response(family: JobFamily, parents: dict[uuid.UUID, JobFamily]) -> JobFamilyResponse:
    return JobFamilyResponse(
        id=family.id,
        organization_id=family.organization_id,
        family_code=family.family_code,
        name=family.name,
        description=family.description,
        node_level=family.node_level,
        parent_job_family_id=family.parent_job_family_id,
        display_order=family.display_order,
        status=family.status,
        row_version=family.row_version,
        archived_at=family.archived_at,
        parent=_family_summary(
            parents.get(family.parent_job_family_id) if family.parent_job_family_id else None
        ),
    )


def _grade_summary(grades: dict[uuid.UUID, Any], grade_id: uuid.UUID | None) -> GradeSummary | None:
    """A nullable grade reference, resolved from an already-loaded batch.

    Takes the id rather than the row so a job with no grade needs no branch at the call
    site, and so no caller is tempted to fetch one grade at a time.
    """
    grade = grades.get(grade_id) if grade_id is not None else None
    if grade is None:
        return None
    return GradeSummary(id=grade.id, code=grade.code, name=grade.name, rank=grade.rank)


def _job_summary(job: Job) -> JobSummary:
    return JobSummary(
        id=job.id,
        job_code=job.job_code,
        title=job.title,
        job_family_id=job.job_family_id,
        default_grade_id=job.default_grade_id,
        status=job.status,
        effective_from=job.effective_from,
        effective_to=job.effective_to,
        row_version=job.row_version,
    )


def _requirement_response(
    requirement: EffectiveRequirement, competencies: dict[uuid.UUID, Competency]
) -> CompetencyRequirementResponse:
    competency = competencies.get(requirement.competency_id)
    return CompetencyRequirementResponse(
        id=requirement.mapping_id,
        competency_id=requirement.competency_id,
        competency_code=competency.competency_code if competency else None,
        competency_name=competency.name if competency else None,
        requirement_type=requirement.requirement_type,
        min_proficiency=requirement.min_proficiency,
        display_order=requirement.display_order,
        effective_from=requirement.effective_from,
        effective_to=requirement.effective_to,
        row_version=requirement.row_version,
        source=requirement.source,
    )


def _requirements(
    service: CompetencyService,
    requirements: list[EffectiveRequirement],
) -> list[CompetencyRequirementResponse]:
    """Resolve competency labels in one batched lookup, never one query per requirement."""
    competencies = service.competency_labels(
        [requirement.competency_id for requirement in requirements]
    )
    return [_requirement_response(row, competencies) for row in requirements]


def _job_response(
    job: Job,
    *,
    catalogue: JobCatalogueService,
    competency_service: CompetencyService,
    context: CurrentOrg,
) -> JobResponse:
    families = catalogue.family_summaries([job.job_family_id])
    grades = catalogue.grades_for([job.default_grade_id, job.min_grade_id, job.max_grade_id])
    requirements = competency_service.job_requirements(context, job_id=job.id)
    return JobResponse(
        id=job.id,
        organization_id=job.organization_id,
        job_code=job.job_code,
        title=job.title,
        description=job.description,
        responsibilities=job.responsibilities,
        job_family_id=job.job_family_id,
        default_grade_id=job.default_grade_id,
        min_grade_id=job.min_grade_id,
        max_grade_id=job.max_grade_id,
        default_designation_id=job.default_designation_id,
        default_employment_type_id=job.default_employment_type_id,
        min_experience_years=job.min_experience_years,
        required_qualifications=job.required_qualifications,
        status=job.status,
        effective_from=job.effective_from,
        effective_to=job.effective_to,
        row_version=job.row_version,
        archived_at=job.archived_at,
        family=_family_summary(families.get(job.job_family_id)),
        default_grade=_grade_summary(grades, job.default_grade_id),
        min_grade=_grade_summary(grades, job.min_grade_id),
        max_grade=_grade_summary(grades, job.max_grade_id),
        competencies=_requirements(competency_service, requirements),
    )


def _competency_response(competency: Competency) -> CompetencyResponse:
    return CompetencyResponse(
        id=competency.id,
        organization_id=competency.organization_id,
        competency_code=competency.competency_code,
        name=competency.name,
        description=competency.description,
        category_code=competency.category_code,
        status=competency.status,
        row_version=competency.row_version,
        archived_at=competency.archived_at,
    )


# ======================================================================= families


@router.get(
    "/job-families",
    response_model=Page[JobFamilyResponse],
    summary="List and search job families",
)
def list_job_families(
    context: CurrentOrg,
    service: Annotated[JobCatalogueService, Depends(get_job_catalogue_service)],
    codec: Annotated[CursorCodec, Depends(get_cursor_codec)],
    q: Annotated[str | None, Query(min_length=2, max_length=100)] = None,
    node_level: Annotated[str | None, Query()] = None,
    parent_id: uuid.UUID | None = None,
    status_filter: Annotated[list[str] | None, Query(alias="status")] = None,
    include_archived: bool = False,
    sort: str | None = None,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    with_total: bool = False,
) -> Page[JobFamilyResponse]:
    spec = SortSpec.parse(sort, allowed=FAMILY_SORT_FIELDS, default_field="display_order")
    after_value, after_id, direction = _decode_cursor(codec, cursor, spec)
    rows, has_more, total = service.list_families(
        context,
        query=q,
        node_level=node_level,
        parent_id=parent_id,
        status=status_filter,
        include_archived=include_archived,
        sort_field=spec.field,
        direction=direction.value,
        after_value=after_value,
        after_id=after_id,
        limit=limit,
        with_total=with_total,
    )
    parents = service.family_summaries(
        [row.parent_job_family_id for row in rows if row.parent_job_family_id]
    )
    return Page[JobFamilyResponse](
        items=[_family_response(row, parents) for row in rows],
        next_cursor=next_cursor_for(
            codec,
            rows=rows,
            has_more=has_more,
            sort_field=spec.field,
            direction=direction,
            value_of=lambda row: getattr(row, _family_sort_column(spec.field)),
            id_of=lambda row: row.id,
        ),
        total_count=total,
    )


@router.post(
    "/job-families",
    response_model=JobFamilyResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a job family or subfamily",
)
def create_job_family(
    payload: JobFamilyCreateRequest,
    response: Response,
    context: CurrentOrg,
    db: DbSession,
    service: Annotated[JobCatalogueService, Depends(get_job_catalogue_service)],
) -> JobFamilyResponse:
    values = payload.model_dump(exclude={"acknowledged_warnings"})
    with unit_of_work(db):
        family, _ = service.create_family(
            context, values=values, acknowledged_warnings=payload.acknowledged_warnings
        )
        result = _family_response(family, service.family_summaries([family.parent_job_family_id]))
    return _finalise(response, family, result)


@router.get(
    "/job-families/{family_id}",
    response_model=JobFamilyResponse,
    summary="Read a job family",
)
def read_job_family(
    family_id: uuid.UUID,
    response: Response,
    context: CurrentOrg,
    service: Annotated[JobCatalogueService, Depends(get_job_catalogue_service)],
) -> JobFamilyResponse:
    family = service.get_family(context, family_id)
    _etag(response, family.row_version)
    return _family_response(family, service.family_summaries([family.parent_job_family_id]))


@router.patch(
    "/job-families/{family_id}",
    response_model=JobFamilyResponse,
    summary="Edit or reparent a job family",
)
def update_job_family(
    family_id: uuid.UUID,
    payload: JobFamilyUpdateRequest,
    response: Response,
    context: CurrentOrg,
    if_match: IfMatch,
    db: DbSession,
    service: Annotated[JobCatalogueService, Depends(get_job_catalogue_service)],
) -> JobFamilyResponse:
    changes = payload.model_dump(exclude_unset=True, exclude={"acknowledged_warnings"})
    with unit_of_work(db):
        family, _ = service.update_family(
            context,
            family_id=family_id,
            changes=changes,
            if_match=if_match,
            acknowledged_warnings=payload.acknowledged_warnings,
        )
        result = _family_response(family, service.family_summaries([family.parent_job_family_id]))
    return _finalise(response, family, result)


@router.post(
    "/job-families/{family_id}/deactivate",
    response_model=JobFamilyResponse,
    summary="Deactivate a job family",
)
def deactivate_job_family(
    family_id: uuid.UUID,
    payload: ReasonRequest,
    response: Response,
    context: CurrentOrg,
    if_match: IfMatch,
    db: DbSession,
    service: Annotated[JobCatalogueService, Depends(get_job_catalogue_service)],
) -> JobFamilyResponse:
    with unit_of_work(db):
        family = service.set_family_status(
            context,
            family_id=family_id,
            active=False,
            reason=payload.reason,
            if_match=if_match,
        )
        result = _family_response(family, service.family_summaries([family.parent_job_family_id]))
    return _finalise(response, family, result)


@router.post(
    "/job-families/{family_id}/reactivate",
    response_model=JobFamilyResponse,
    summary="Reactivate a job family",
)
def reactivate_job_family(
    family_id: uuid.UUID,
    payload: ReasonRequest,
    response: Response,
    context: CurrentOrg,
    if_match: IfMatch,
    db: DbSession,
    service: Annotated[JobCatalogueService, Depends(get_job_catalogue_service)],
) -> JobFamilyResponse:
    with unit_of_work(db):
        family = service.set_family_status(
            context,
            family_id=family_id,
            active=True,
            reason=payload.reason,
            if_match=if_match,
        )
        result = _family_response(family, service.family_summaries([family.parent_job_family_id]))
    return _finalise(response, family, result)


@router.post(
    "/job-families/{family_id}/archive",
    response_model=JobFamilyResponse,
    summary="Archive a job family",
)
def archive_job_family(
    family_id: uuid.UUID,
    payload: ReasonRequest,
    response: Response,
    context: CurrentOrg,
    if_match: IfMatch,
    db: DbSession,
    service: Annotated[JobCatalogueService, Depends(get_job_catalogue_service)],
) -> JobFamilyResponse:
    with unit_of_work(db):
        family = service.archive_family(
            context, family_id=family_id, reason=payload.reason, if_match=if_match
        )
        result = _family_response(family, service.family_summaries([family.parent_job_family_id]))
    return _finalise(response, family, result)


@router.post(
    "/job-families/{family_id}/restore",
    response_model=JobFamilyResponse,
    summary="Restore an archived job family to INACTIVE",
)
def restore_job_family(
    family_id: uuid.UUID,
    payload: ReasonRequest,
    response: Response,
    context: CurrentOrg,
    if_match: IfMatch,
    db: DbSession,
    service: Annotated[JobCatalogueService, Depends(get_job_catalogue_service)],
) -> JobFamilyResponse:
    with unit_of_work(db):
        family = service.restore_family(
            context, family_id=family_id, reason=payload.reason, if_match=if_match
        )
        result = _family_response(family, service.family_summaries([family.parent_job_family_id]))
    return _finalise(response, family, result)


# =========================================================================== jobs


@router.get(
    "/job-catalog", response_model=Page[JobSummary], summary="List and search the job catalogue"
)
def list_jobs(
    context: CurrentOrg,
    service: Annotated[JobCatalogueService, Depends(get_job_catalogue_service)],
    codec: Annotated[CursorCodec, Depends(get_cursor_codec)],
    q: Annotated[str | None, Query(min_length=2, max_length=100)] = None,
    job_family_id: uuid.UUID | None = None,
    include_subfamilies: bool = True,
    grade_id: uuid.UUID | None = None,
    employment_type_id: uuid.UUID | None = None,
    status_filter: Annotated[list[str] | None, Query(alias="status")] = None,
    effective_on: date | None = None,
    include_archived: bool = False,
    sort: str | None = None,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    with_total: bool = False,
) -> Page[JobSummary]:
    spec = SortSpec.parse(sort, allowed=JOB_SORT_FIELDS, default_field="title")
    after_value, after_id, direction = _decode_cursor(codec, cursor, spec)
    rows, has_more, total = service.list_jobs(
        context,
        query=q,
        job_family_id=job_family_id,
        include_subfamilies=include_subfamilies,
        grade_id=grade_id,
        employment_type_id=employment_type_id,
        status=status_filter,
        effective_on=effective_on,
        include_archived=include_archived,
        sort_field=spec.field,
        direction=direction.value,
        after_value=after_value,
        after_id=after_id,
        limit=limit,
        with_total=with_total,
    )
    return Page[JobSummary](
        items=[_job_summary(row) for row in rows],
        next_cursor=next_cursor_for(
            codec,
            rows=rows,
            has_more=has_more,
            sort_field=spec.field,
            direction=direction,
            value_of=lambda row: getattr(row, _job_sort_column(spec.field)),
            id_of=lambda row: row.id,
        ),
        total_count=total,
    )


@router.post(
    "/job-catalog",
    response_model=JobResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a job",
)
def create_job(
    payload: JobCreateRequest,
    response: Response,
    context: CurrentOrg,
    db: DbSession,
    service: Annotated[JobCatalogueService, Depends(get_job_catalogue_service)],
    competencies: Annotated[CompetencyService, Depends(get_competency_service)],
) -> JobResponse:
    values = payload.model_dump(exclude={"acknowledged_warnings", "competencies"})
    with unit_of_work(db):
        job, _ = service.create_job(
            context, values=values, acknowledged_warnings=payload.acknowledged_warnings
        )
        if payload.competencies:
            competencies.seed_initial_mappings(
                context,
                owner_field="job_id",
                owner_id=job.id,
                mappings=[item.model_dump() for item in payload.competencies],
            )
        result = _job_response(
            job, catalogue=service, competency_service=competencies, context=context
        )
    return _finalise(response, job, result)


@router.get("/job-catalog/{job_id}", response_model=JobResponse, summary="Read a job")
def read_job(
    job_id: uuid.UUID,
    response: Response,
    context: CurrentOrg,
    service: Annotated[JobCatalogueService, Depends(get_job_catalogue_service)],
    competencies: Annotated[CompetencyService, Depends(get_competency_service)],
) -> JobResponse:
    job = service.get_job(context, job_id)
    _etag(response, job.row_version)
    return _job_response(job, catalogue=service, competency_service=competencies, context=context)


@router.patch("/job-catalog/{job_id}", response_model=JobResponse, summary="Edit a job")
def update_job(
    job_id: uuid.UUID,
    payload: JobUpdateRequest,
    response: Response,
    context: CurrentOrg,
    if_match: IfMatch,
    db: DbSession,
    service: Annotated[JobCatalogueService, Depends(get_job_catalogue_service)],
    competencies: Annotated[CompetencyService, Depends(get_competency_service)],
) -> JobResponse:
    changes = payload.model_dump(
        exclude_unset=True, exclude={"acknowledged_warnings", "competencies"}
    )
    with unit_of_work(db):
        job, _ = service.update_job(
            context,
            job_id=job_id,
            changes=changes,
            if_match=if_match,
            acknowledged_warnings=payload.acknowledged_warnings,
        )
        if payload.competencies is not None:
            competencies.replace_job_requirements(
                context,
                job_id=job.id,
                mappings=[item.model_dump() for item in payload.competencies],
                reason=None,
                if_match=job.row_version,
                acknowledged_warnings=payload.acknowledged_warnings,
            )
        result = _job_response(
            job, catalogue=service, competency_service=competencies, context=context
        )
    return _finalise(response, job, result)


@router.post(
    "/job-catalog/{job_id}/deactivate", response_model=JobResponse, summary="Deactivate a job"
)
def deactivate_job(
    job_id: uuid.UUID,
    payload: ReasonRequest,
    response: Response,
    context: CurrentOrg,
    if_match: IfMatch,
    db: DbSession,
    service: Annotated[JobCatalogueService, Depends(get_job_catalogue_service)],
    competencies: Annotated[CompetencyService, Depends(get_competency_service)],
) -> JobResponse:
    with unit_of_work(db):
        job = service.set_job_status(
            context, job_id=job_id, active=False, reason=payload.reason, if_match=if_match
        )
        result = _job_response(
            job, catalogue=service, competency_service=competencies, context=context
        )
    return _finalise(response, job, result)


@router.post(
    "/job-catalog/{job_id}/reactivate", response_model=JobResponse, summary="Reactivate a job"
)
def reactivate_job(
    job_id: uuid.UUID,
    payload: ReasonRequest,
    response: Response,
    context: CurrentOrg,
    if_match: IfMatch,
    db: DbSession,
    service: Annotated[JobCatalogueService, Depends(get_job_catalogue_service)],
    competencies: Annotated[CompetencyService, Depends(get_competency_service)],
) -> JobResponse:
    with unit_of_work(db):
        job = service.set_job_status(
            context, job_id=job_id, active=True, reason=payload.reason, if_match=if_match
        )
        result = _job_response(
            job, catalogue=service, competency_service=competencies, context=context
        )
    return _finalise(response, job, result)


@router.post("/job-catalog/{job_id}/archive", response_model=JobResponse, summary="Archive a job")
def archive_job(
    job_id: uuid.UUID,
    payload: ReasonRequest,
    response: Response,
    context: CurrentOrg,
    if_match: IfMatch,
    db: DbSession,
    service: Annotated[JobCatalogueService, Depends(get_job_catalogue_service)],
    competencies: Annotated[CompetencyService, Depends(get_competency_service)],
) -> JobResponse:
    with unit_of_work(db):
        job = service.archive_job(context, job_id=job_id, reason=payload.reason, if_match=if_match)
        result = _job_response(
            job, catalogue=service, competency_service=competencies, context=context
        )
    return _finalise(response, job, result)


@router.post(
    "/job-catalog/{job_id}/restore",
    response_model=JobResponse,
    summary="Restore an archived job to INACTIVE",
)
def restore_job(
    job_id: uuid.UUID,
    payload: ReasonRequest,
    response: Response,
    context: CurrentOrg,
    if_match: IfMatch,
    db: DbSession,
    service: Annotated[JobCatalogueService, Depends(get_job_catalogue_service)],
    competencies: Annotated[CompetencyService, Depends(get_competency_service)],
) -> JobResponse:
    with unit_of_work(db):
        job = service.restore_job(context, job_id=job_id, reason=payload.reason, if_match=if_match)
        result = _job_response(
            job, catalogue=service, competency_service=competencies, context=context
        )
    return _finalise(response, job, result)


@router.get(
    "/job-catalog/{job_id}/competencies",
    response_model=list[CompetencyRequirementResponse],
    summary="Read a job's competency requirements",
)
def read_job_competencies(
    job_id: uuid.UUID,
    context: CurrentOrg,
    service: Annotated[CompetencyService, Depends(get_competency_service)],
    as_of: date | None = None,
) -> list[CompetencyRequirementResponse]:
    return _requirements(service, service.job_requirements(context, job_id=job_id, as_of=as_of))


@router.put(
    "/job-catalog/{job_id}/competencies",
    response_model=list[CompetencyRequirementResponse],
    summary="Replace a job's competency requirements",
)
def replace_job_competencies(
    job_id: uuid.UUID,
    payload: CompetencyMappingReplaceRequest,
    context: CurrentOrg,
    if_match: IfMatch,
    db: DbSession,
    service: Annotated[CompetencyService, Depends(get_competency_service)],
) -> list[CompetencyRequirementResponse]:
    with unit_of_work(db):
        requirements = service.replace_job_requirements(
            context,
            job_id=job_id,
            mappings=[item.model_dump() for item in payload.mappings],
            reason=payload.reason,
            if_match=if_match,
            acknowledged_warnings=payload.acknowledged_warnings,
        )
        return _requirements(service, requirements)


# =================================================================== competencies


@router.get(
    "/competencies",
    response_model=Page[CompetencyResponse],
    summary="List and search competencies",
)
def list_competencies(
    context: CurrentOrg,
    service: Annotated[CompetencyService, Depends(get_competency_service)],
    codec: Annotated[CursorCodec, Depends(get_cursor_codec)],
    q: Annotated[str | None, Query(min_length=2, max_length=100)] = None,
    category_code: str | None = None,
    status_filter: Annotated[list[str] | None, Query(alias="status")] = None,
    include_archived: bool = False,
    sort: str | None = None,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    with_total: bool = False,
) -> Page[CompetencyResponse]:
    spec = SortSpec.parse(sort, allowed=COMPETENCY_SORT_FIELDS, default_field="name")
    after_value, after_id, direction = _decode_cursor(codec, cursor, spec)
    rows, has_more, total = service.list_competencies(
        context,
        query=q,
        category_code=category_code,
        status=status_filter,
        include_archived=include_archived,
        sort_field=spec.field,
        direction=direction.value,
        after_value=after_value,
        after_id=after_id,
        limit=limit,
        with_total=with_total,
    )
    return Page[CompetencyResponse](
        items=[_competency_response(row) for row in rows],
        next_cursor=next_cursor_for(
            codec,
            rows=rows,
            has_more=has_more,
            sort_field=spec.field,
            direction=direction,
            value_of=lambda row: getattr(row, _competency_sort_column(spec.field)),
            id_of=lambda row: row.id,
        ),
        total_count=total,
    )


@router.post(
    "/competencies",
    response_model=CompetencyResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a competency",
)
def create_competency(
    payload: CompetencyCreateRequest,
    response: Response,
    context: CurrentOrg,
    db: DbSession,
    service: Annotated[CompetencyService, Depends(get_competency_service)],
) -> CompetencyResponse:
    values = payload.model_dump(exclude={"acknowledged_warnings"})
    with unit_of_work(db):
        competency, _ = service.create_competency(
            context, values=values, acknowledged_warnings=payload.acknowledged_warnings
        )
        result = _competency_response(competency)
    return _finalise(response, competency, result)


@router.get(
    "/competencies/{competency_id}",
    response_model=CompetencyResponse,
    summary="Read a competency",
)
def read_competency(
    competency_id: uuid.UUID,
    response: Response,
    context: CurrentOrg,
    service: Annotated[CompetencyService, Depends(get_competency_service)],
) -> CompetencyResponse:
    competency = service.get_competency(context, competency_id)
    _etag(response, competency.row_version)
    return _competency_response(competency)


@router.patch(
    "/competencies/{competency_id}",
    response_model=CompetencyResponse,
    summary="Edit a competency",
)
def update_competency(
    competency_id: uuid.UUID,
    payload: CompetencyUpdateRequest,
    response: Response,
    context: CurrentOrg,
    if_match: IfMatch,
    db: DbSession,
    service: Annotated[CompetencyService, Depends(get_competency_service)],
) -> CompetencyResponse:
    changes = payload.model_dump(exclude_unset=True, exclude={"acknowledged_warnings"})
    with unit_of_work(db):
        competency, _ = service.update_competency(
            context,
            competency_id=competency_id,
            changes=changes,
            if_match=if_match,
            acknowledged_warnings=payload.acknowledged_warnings,
        )
        result = _competency_response(competency)
    return _finalise(response, competency, result)


@router.post(
    "/competencies/{competency_id}/deactivate",
    response_model=CompetencyResponse,
    summary="Deactivate a competency",
)
def deactivate_competency(
    competency_id: uuid.UUID,
    payload: ReasonRequest,
    response: Response,
    context: CurrentOrg,
    if_match: IfMatch,
    db: DbSession,
    service: Annotated[CompetencyService, Depends(get_competency_service)],
) -> CompetencyResponse:
    with unit_of_work(db):
        competency = service.set_competency_status(
            context,
            competency_id=competency_id,
            active=False,
            reason=payload.reason,
            if_match=if_match,
        )
        result = _competency_response(competency)
    return _finalise(response, competency, result)


@router.post(
    "/competencies/{competency_id}/reactivate",
    response_model=CompetencyResponse,
    summary="Reactivate a competency",
)
def reactivate_competency(
    competency_id: uuid.UUID,
    payload: ReasonRequest,
    response: Response,
    context: CurrentOrg,
    if_match: IfMatch,
    db: DbSession,
    service: Annotated[CompetencyService, Depends(get_competency_service)],
) -> CompetencyResponse:
    with unit_of_work(db):
        competency = service.set_competency_status(
            context,
            competency_id=competency_id,
            active=True,
            reason=payload.reason,
            if_match=if_match,
        )
        result = _competency_response(competency)
    return _finalise(response, competency, result)


@router.post(
    "/competencies/{competency_id}/archive",
    response_model=CompetencyResponse,
    summary="Archive a competency",
)
def archive_competency(
    competency_id: uuid.UUID,
    payload: ReasonRequest,
    response: Response,
    context: CurrentOrg,
    if_match: IfMatch,
    db: DbSession,
    service: Annotated[CompetencyService, Depends(get_competency_service)],
) -> CompetencyResponse:
    with unit_of_work(db):
        competency = service.archive_competency(
            context, competency_id=competency_id, reason=payload.reason, if_match=if_match
        )
        result = _competency_response(competency)
    return _finalise(response, competency, result)


@router.post(
    "/competencies/{competency_id}/restore",
    response_model=CompetencyResponse,
    summary="Restore an archived competency to INACTIVE",
)
def restore_competency(
    competency_id: uuid.UUID,
    payload: ReasonRequest,
    response: Response,
    context: CurrentOrg,
    if_match: IfMatch,
    db: DbSession,
    service: Annotated[CompetencyService, Depends(get_competency_service)],
) -> CompetencyResponse:
    with unit_of_work(db):
        competency = service.restore_competency(
            context, competency_id=competency_id, reason=payload.reason, if_match=if_match
        )
        result = _competency_response(competency)
    return _finalise(response, competency, result)


# ----------------------------------------------------------------- cursor plumbing


def _decode_cursor(
    codec: CursorCodec, cursor: str | None, spec: SortSpec
) -> tuple[Any, uuid.UUID | None, SortDirection]:
    """Decode a cursor and refuse one issued for a different sort (§26.5)."""
    if cursor is None:
        return None, None, spec.direction
    payload = codec.decode(cursor)
    if payload.sort_field != spec.field:
        raise ValidationFailed("The cursor was issued for a different sort.")
    return payload.sort_value, payload.last_id, payload.direction


def _family_sort_column(field: str) -> str:
    return {
        "family_code": "family_code_normalized",
        "name": "name",
        "display_order": "display_order",
        "created_at": "created_at",
    }[field]


def _job_sort_column(field: str) -> str:
    return {
        "job_code": "job_code_normalized",
        "title": "title",
        "created_at": "created_at",
        "effective_from": "effective_from",
    }[field]


def _competency_sort_column(field: str) -> str:
    return {
        "competency_code": "competency_code_normalized",
        "name": "name",
        "category_code": "category_code",
        "created_at": "created_at",
    }[field]


__all__ = ["router"]
