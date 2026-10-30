"""Headcount summaries, plans, change requests and reservations (JA §13.8, §13.9).

Everything here is gated on ``headcount.view`` for reads and ``headcount.manage`` /
``headcount.approve`` for writes — never on ``employee.view``. JA §12.1 treats workforce
planning as confidential in its own right: a caller who may read employees must not thereby
learn the organization's establishment, and a caller who may read headcount gets counts
without incumbent identity.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, Response, status

from app.api.deps import (
    CurrentOrg,
    DbSession,
    IdempotencyKeyHeader,
    IfMatch,
    get_cursor_codec,
    get_headcount_service,
    get_idempotency_repository,
)
from app.api.idempotency import idempotency_guard, require_idempotency_key
from app.core.errors import ValidationFailed
from app.db.session import unit_of_work
from app.models.position import HeadcountChangeRequest, HeadcountPlan, HeadcountReservation
from app.repositories.platform import IdempotencyRepository
from app.repositories.position import SummaryRow
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
    HeadcountChangeRequestCreate,
    HeadcountChangeRequestResponse,
    HeadcountChangeRequestUpdate,
    HeadcountDecisionRequest,
    HeadcountPlanCreateRequest,
    HeadcountPlanResponse,
    HeadcountPlanUpdateRequest,
    HeadcountReconciliationFinding,
    HeadcountReconciliationResponse,
    HeadcountReservationCreate,
    HeadcountReservationResponse,
    HeadcountSummaryRow,
)
from app.services.headcount import HeadcountService

router = APIRouter(prefix="/orgs/{org_id}", tags=["Headcount"])

PLAN_SORT_FIELDS = frozenset({"plan_code", "name", "effective_from", "created_at"})
REQUEST_SORT_FIELDS = frozenset({"created_at", "request_status"})

ZERO = Decimal("0.00")


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


def _plan_response(
    plan: HeadcountPlan,
    *,
    committed_fte: Decimal | None = None,
    committed_headcount: int | None = None,
) -> HeadcountPlanResponse:
    payload = HeadcountPlanResponse(
        id=plan.id,
        organization_id=plan.organization_id,
        plan_code=plan.plan_code,
        name=plan.name,
        description=plan.description,
        scope_type=plan.scope_type,
        department_id=plan.department_id,
        branch_id=plan.branch_id,
        work_location_id=plan.work_location_id,
        job_id=plan.job_id,
        grade_id=plan.grade_id,
        authorized_fte=plan.authorized_fte,
        authorized_headcount=plan.authorized_headcount,
        plan_status=plan.plan_status,
        effective_from=plan.effective_from,
        effective_to=plan.effective_to,
        approved_by=plan.approved_by,
        approved_at=plan.approved_at,
        row_version=plan.row_version,
    )
    if committed_fte is not None and committed_headcount is not None:
        payload.plan_authorized_fte = committed_fte
        payload.plan_available_fte = max(ZERO, plan.authorized_fte - committed_fte)
        payload.plan_authorized_headcount = committed_headcount
        payload.plan_available_headcount = max(0, plan.authorized_headcount - committed_headcount)
    return payload


def _request_response(request: HeadcountChangeRequest) -> HeadcountChangeRequestResponse:
    return HeadcountChangeRequestResponse(
        id=request.id,
        organization_id=request.organization_id,
        position_id=request.position_id,
        headcount_plan_id=request.headcount_plan_id,
        change_type=request.change_type,
        current_fte=request.current_fte,
        requested_fte=request.requested_fte,
        current_headcount=request.current_headcount,
        requested_headcount=request.requested_headcount,
        reason=request.reason,
        request_status=request.request_status,
        submitted_at=request.submitted_at,
        submitted_by=request.submitted_by,
        decided_by=request.decided_by,
        decided_at=request.decided_at,
        decision_note=request.decision_note,
        applied_at=request.applied_at,
        row_version=request.row_version,
    )


def _reservation_response(row: HeadcountReservation) -> HeadcountReservationResponse:
    return HeadcountReservationResponse(
        id=row.id,
        organization_id=row.organization_id,
        position_id=row.position_id,
        source_type=row.source_type,
        source_reference=row.source_reference,
        reserved_fte=row.reserved_fte,
        reserved_headcount=row.reserved_headcount,
        reservation_status=row.reservation_status,
        effective_from=row.effective_from,
        expires_at=row.expires_at,
        reason=row.reason,
        row_version=row.row_version,
    )


def _summary_row(row: SummaryRow, as_of: date) -> HeadcountSummaryRow:
    vacant_fte = max(ZERO, row.authorized_fte - row.occupied_fte - row.reserved_fte)
    vacant_headcount = max(
        0, row.authorized_headcount - row.occupied_headcount - row.reserved_headcount
    )
    return HeadcountSummaryRow(
        group_by=row.group_key or "ORGANIZATION",
        group_id=row.group_id,
        group_label=row.group_label,
        as_of=as_of,
        authorized_fte=row.authorized_fte,
        occupied_fte=row.occupied_fte,
        reserved_fte=row.reserved_fte,
        vacant_fte=vacant_fte,
        authorized_headcount=row.authorized_headcount,
        occupied_headcount=row.occupied_headcount,
        reserved_headcount=row.reserved_headcount,
        vacant_headcount=vacant_headcount,
        position_count=row.position_count,
        open_position_count=row.open_position_count,
        # JA §8.10: over_capacity must be zero. A non-zero value here is a defect, which is
        # why it is reported rather than clamped away.
        over_capacity=1
        if row.occupied_fte > row.authorized_fte
        or row.occupied_headcount > row.authorized_headcount
        else 0,
        under_capacity=1 if vacant_fte > 0 or vacant_headcount > 0 else 0,
    )


# ====================================================================== reports


@router.get(
    "/headcount/summary",
    response_model=Page[HeadcountSummaryRow],
    summary="Authorized versus occupied versus vacant headcount",
)
def headcount_summary(
    context: CurrentOrg,
    service: Annotated[HeadcountService, Depends(get_headcount_service)],
    as_of: date | None = None,
    group_by: Annotated[str, Query()] = "ORGANIZATION",
    department_id: uuid.UUID | None = None,
    branch_id: uuid.UUID | None = None,
    job_id: uuid.UUID | None = None,
    grade_id: uuid.UUID | None = None,
    position_id: uuid.UUID | None = None,
    include_vacant_only: bool = False,
    include_draft: bool = False,
) -> Page[HeadcountSummaryRow]:
    """JA §8.10. FTE values are two-decimal strings (JA-Q-13); no personal data appears."""
    moment, rows = service.summary(
        context,
        group_by=group_by,
        as_of=as_of,
        department_id=department_id,
        branch_id=branch_id,
        job_id=job_id,
        grade_id=grade_id,
        position_id=position_id,
        include_vacant_only=include_vacant_only,
        include_draft=include_draft,
    )
    return Page[HeadcountSummaryRow](items=[_summary_row(row, moment) for row in rows])


@router.get(
    "/headcount/reconciliation",
    response_model=HeadcountReconciliationResponse,
    summary="Over- and under-capacity findings",
)
def headcount_reconciliation(
    context: CurrentOrg,
    service: Annotated[HeadcountService, Depends(get_headcount_service)],
    as_of: date | None = None,
) -> HeadcountReconciliationResponse:
    """JA §17.5. An empty ``findings`` list is the expected result."""
    moment, findings = service.reconciliation(context, as_of=as_of)
    return HeadcountReconciliationResponse(
        as_of=moment,
        findings=[HeadcountReconciliationFinding(**finding) for finding in findings],
    )


# ======================================================================== plans


@router.get(
    "/headcount-plans", response_model=Page[HeadcountPlanResponse], summary="List headcount plans"
)
def list_plans(
    context: CurrentOrg,
    service: Annotated[HeadcountService, Depends(get_headcount_service)],
    codec: Annotated[CursorCodec, Depends(get_cursor_codec)],
    scope_type: str | None = None,
    scope_id: uuid.UUID | None = None,
    plan_status: Annotated[list[str] | None, Query()] = None,
    effective_on: date | None = None,
    include_archived: bool = False,
    sort: str | None = None,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    with_total: bool = False,
) -> Page[HeadcountPlanResponse]:
    spec = SortSpec.parse(
        sort,
        allowed=PLAN_SORT_FIELDS,
        default_field="effective_from",
        default_direction=SortDirection.DESC,
    )
    after_value, after_id, direction = _decode_cursor(codec, cursor, spec)
    rows, has_more, total = service.list_plans(
        context,
        scope_type=scope_type,
        scope_id=scope_id,
        plan_status=plan_status,
        effective_on=effective_on,
        include_archived=include_archived,
        sort_field=spec.field,
        direction=direction.value,
        after_value=after_value,
        after_id=after_id,
        limit=limit,
        with_total=with_total,
    )
    return Page[HeadcountPlanResponse](
        items=[_plan_response(row) for row in rows],
        next_cursor=next_cursor_for(
            codec,
            rows=rows,
            has_more=has_more,
            sort_field=spec.field,
            direction=direction,
            value_of=lambda row: getattr(row, _plan_sort_column(spec.field)),
            id_of=lambda row: row.id,
        ),
        total_count=total,
    )


@router.post(
    "/headcount-plans",
    response_model=HeadcountPlanResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a DRAFT headcount plan",
)
def create_plan(
    payload: HeadcountPlanCreateRequest,
    request: Request,
    response: Response,
    context: CurrentOrg,
    db: DbSession,
    idempotency_key: IdempotencyKeyHeader,
    service: Annotated[HeadcountService, Depends(get_headcount_service)],
    idempotency: Annotated[IdempotencyRepository, Depends(get_idempotency_repository)],
) -> HeadcountPlanResponse:
    key = require_idempotency_key(idempotency_key, endpoint="Creating a headcount plan")
    guard = idempotency_guard(
        repository=idempotency,
        context=context,
        key=key,
        method="POST",
        path=str(request.url.path),
        payload=payload.model_dump(mode="json"),
    )
    replayed = guard.replay()
    if replayed is not None:
        return HeadcountPlanResponse.model_validate(replayed)

    with unit_of_work(db):
        guard.claim()
        plan = service.create_plan(context, values=payload.model_dump())
        result = _plan_response(plan)
        guard.complete(status_code=status.HTTP_201_CREATED, body=result.model_dump(mode="json"))
    return _finalise(response, plan, result)


@router.get(
    "/headcount-plans/{plan_id}",
    response_model=HeadcountPlanResponse,
    summary="Read a headcount plan and the establishment it covers",
)
def read_plan(
    plan_id: uuid.UUID,
    response: Response,
    context: CurrentOrg,
    service: Annotated[HeadcountService, Depends(get_headcount_service)],
) -> HeadcountPlanResponse:
    plan, committed_fte, committed_headcount = service.plan_availability(context, plan_id)
    _etag(response, plan.row_version)
    return _plan_response(
        plan, committed_fte=committed_fte, committed_headcount=committed_headcount
    )


@router.patch(
    "/headcount-plans/{plan_id}",
    response_model=HeadcountPlanResponse,
    summary="Edit a DRAFT headcount plan",
)
def update_plan(
    plan_id: uuid.UUID,
    payload: HeadcountPlanUpdateRequest,
    response: Response,
    context: CurrentOrg,
    if_match: IfMatch,
    db: DbSession,
    service: Annotated[HeadcountService, Depends(get_headcount_service)],
) -> HeadcountPlanResponse:
    with unit_of_work(db):
        plan = service.update_plan(
            context,
            plan_id=plan_id,
            changes=payload.model_dump(exclude_unset=True),
            if_match=if_match,
        )
        result = _plan_response(plan)
    return _finalise(response, plan, result)


@router.post(
    "/headcount-plans/{plan_id}/activate",
    response_model=HeadcountPlanResponse,
    summary="Approve and activate a headcount plan",
)
def activate_plan(
    plan_id: uuid.UUID,
    payload: HeadcountDecisionRequest,
    response: Response,
    context: CurrentOrg,
    if_match: IfMatch,
    db: DbSession,
    service: Annotated[HeadcountService, Depends(get_headcount_service)],
) -> HeadcountPlanResponse:
    """Needs ``headcount.approve`` and an approver other than the creator (JA §15.4)."""
    with unit_of_work(db):
        plan = service.activate_plan(context, plan_id=plan_id, note=payload.note, if_match=if_match)
        result = _plan_response(plan)
    return _finalise(response, plan, result)


@router.post(
    "/headcount-plans/{plan_id}/archive",
    response_model=HeadcountPlanResponse,
    summary="Archive a headcount plan",
)
def archive_plan(
    plan_id: uuid.UUID,
    payload: ReasonRequest,
    response: Response,
    context: CurrentOrg,
    if_match: IfMatch,
    db: DbSession,
    service: Annotated[HeadcountService, Depends(get_headcount_service)],
) -> HeadcountPlanResponse:
    with unit_of_work(db):
        plan = service.archive_plan(
            context, plan_id=plan_id, reason=payload.reason, if_match=if_match
        )
        result = _plan_response(plan)
    return _finalise(response, plan, result)


# ============================================================== change requests


@router.get(
    "/headcount-change-requests",
    response_model=Page[HeadcountChangeRequestResponse],
    summary="List headcount change requests",
)
def list_requests(
    context: CurrentOrg,
    service: Annotated[HeadcountService, Depends(get_headcount_service)],
    codec: Annotated[CursorCodec, Depends(get_cursor_codec)],
    request_status: Annotated[list[str] | None, Query()] = None,
    target_type: Annotated[str | None, Query()] = None,
    position_id: uuid.UUID | None = None,
    headcount_plan_id: uuid.UUID | None = None,
    sort: str | None = None,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    with_total: bool = False,
) -> Page[HeadcountChangeRequestResponse]:
    spec = SortSpec.parse(
        sort,
        allowed=REQUEST_SORT_FIELDS,
        default_field="created_at",
        default_direction=SortDirection.DESC,
    )
    after_value, after_id, direction = _decode_cursor(codec, cursor, spec)
    rows, has_more, total = service.list_requests(
        context,
        request_status=request_status,
        target_type=target_type,
        position_id=position_id,
        headcount_plan_id=headcount_plan_id,
        sort_field=spec.field,
        direction=direction.value,
        after_value=after_value,
        after_id=after_id,
        limit=limit,
        with_total=with_total,
    )
    return Page[HeadcountChangeRequestResponse](
        items=[_request_response(row) for row in rows],
        next_cursor=next_cursor_for(
            codec,
            rows=rows,
            has_more=has_more,
            sort_field=spec.field,
            direction=direction,
            value_of=lambda row: getattr(row, spec.field),
            id_of=lambda row: row.id,
        ),
        total_count=total,
    )


@router.post(
    "/headcount-change-requests",
    response_model=HeadcountChangeRequestResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a DRAFT headcount change request",
)
def create_request(
    payload: HeadcountChangeRequestCreate,
    request: Request,
    response: Response,
    context: CurrentOrg,
    db: DbSession,
    idempotency_key: IdempotencyKeyHeader,
    service: Annotated[HeadcountService, Depends(get_headcount_service)],
    idempotency: Annotated[IdempotencyRepository, Depends(get_idempotency_repository)],
) -> HeadcountChangeRequestResponse:
    key = require_idempotency_key(idempotency_key, endpoint="Creating a headcount change request")
    guard = idempotency_guard(
        repository=idempotency,
        context=context,
        key=key,
        method="POST",
        path=str(request.url.path),
        payload=payload.model_dump(mode="json"),
    )
    replayed = guard.replay()
    if replayed is not None:
        return HeadcountChangeRequestResponse.model_validate(replayed)

    with unit_of_work(db):
        guard.claim()
        created = service.create_request(context, values=payload.model_dump())
        result = _request_response(created)
        guard.complete(status_code=status.HTTP_201_CREATED, body=result.model_dump(mode="json"))
    return _finalise(response, created, result)


@router.get(
    "/headcount-change-requests/{request_id}",
    response_model=HeadcountChangeRequestResponse,
    summary="Read a headcount change request",
)
def read_request(
    request_id: uuid.UUID,
    response: Response,
    context: CurrentOrg,
    service: Annotated[HeadcountService, Depends(get_headcount_service)],
) -> HeadcountChangeRequestResponse:
    found = service.get_request(context, request_id)
    _etag(response, found.row_version)
    return _request_response(found)


@router.patch(
    "/headcount-change-requests/{request_id}",
    response_model=HeadcountChangeRequestResponse,
    summary="Edit a DRAFT headcount change request",
)
def update_request(
    request_id: uuid.UUID,
    payload: HeadcountChangeRequestUpdate,
    response: Response,
    context: CurrentOrg,
    if_match: IfMatch,
    db: DbSession,
    service: Annotated[HeadcountService, Depends(get_headcount_service)],
) -> HeadcountChangeRequestResponse:
    with unit_of_work(db):
        updated = service.update_request(
            context,
            request_id=request_id,
            changes=payload.model_dump(exclude_unset=True),
            if_match=if_match,
        )
        result = _request_response(updated)
    return _finalise(response, updated, result)


@router.post(
    "/headcount-change-requests/{request_id}/submit",
    response_model=HeadcountChangeRequestResponse,
    summary="Submit a headcount change request for approval",
)
def submit_request(
    request_id: uuid.UUID,
    response: Response,
    context: CurrentOrg,
    if_match: IfMatch,
    db: DbSession,
    service: Annotated[HeadcountService, Depends(get_headcount_service)],
) -> HeadcountChangeRequestResponse:
    with unit_of_work(db):
        submitted = service.submit_request(context, request_id=request_id, if_match=if_match)
        result = _request_response(submitted)
    return _finalise(response, submitted, result)


@router.post(
    "/headcount-change-requests/{request_id}/approve",
    response_model=HeadcountChangeRequestResponse,
    summary="Approve a headcount change request and apply it",
)
def approve_request(
    request_id: uuid.UUID,
    payload: HeadcountDecisionRequest,
    response: Response,
    context: CurrentOrg,
    if_match: IfMatch,
    db: DbSession,
    service: Annotated[HeadcountService, Depends(get_headcount_service)],
) -> HeadcountChangeRequestResponse:
    """Approval and application commit together (JA §19.10)."""
    with unit_of_work(db):
        approved = service.approve_request(
            context, request_id=request_id, note=payload.note, if_match=if_match
        )
        result = _request_response(approved)
    return _finalise(response, approved, result)


@router.post(
    "/headcount-change-requests/{request_id}/reject",
    response_model=HeadcountChangeRequestResponse,
    summary="Reject a headcount change request",
)
def reject_request(
    request_id: uuid.UUID,
    payload: HeadcountDecisionRequest,
    response: Response,
    context: CurrentOrg,
    if_match: IfMatch,
    db: DbSession,
    service: Annotated[HeadcountService, Depends(get_headcount_service)],
) -> HeadcountChangeRequestResponse:
    if payload.note is None:
        raise ValidationFailed("A rejection requires a note.").with_detail(
            field="note", code="REQUIRED", message="Ten characters or more."
        )
    with unit_of_work(db):
        rejected = service.reject_request(
            context, request_id=request_id, note=payload.note, if_match=if_match
        )
        result = _request_response(rejected)
    return _finalise(response, rejected, result)


@router.post(
    "/headcount-change-requests/{request_id}/cancel",
    response_model=HeadcountChangeRequestResponse,
    summary="Cancel a draft or pending headcount change request",
)
def cancel_request(
    request_id: uuid.UUID,
    payload: ReasonRequest,
    response: Response,
    context: CurrentOrg,
    if_match: IfMatch,
    db: DbSession,
    service: Annotated[HeadcountService, Depends(get_headcount_service)],
) -> HeadcountChangeRequestResponse:
    with unit_of_work(db):
        cancelled = service.cancel_request(
            context, request_id=request_id, reason=payload.reason, if_match=if_match
        )
        result = _request_response(cancelled)
    return _finalise(response, cancelled, result)


# ================================================================ reservations


@router.get(
    "/positions/{position_id}/headcount-reservations",
    response_model=Page[HeadcountReservationResponse],
    summary="List a position's headcount reservations",
)
def list_reservations(
    position_id: uuid.UUID,
    context: CurrentOrg,
    service: Annotated[HeadcountService, Depends(get_headcount_service)],
    reservation_status: Annotated[list[str] | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
) -> Page[HeadcountReservationResponse]:
    rows = service.list_reservations(
        context, position_id=position_id, reservation_status=reservation_status
    )
    return Page[HeadcountReservationResponse](
        items=[_reservation_response(row) for row in rows[:limit]]
    )


@router.post(
    "/positions/{position_id}/headcount-reservations",
    response_model=HeadcountReservationResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Reserve capacity against a position",
)
def create_reservation(
    position_id: uuid.UUID,
    payload: HeadcountReservationCreate,
    request: Request,
    response: Response,
    context: CurrentOrg,
    db: DbSession,
    idempotency_key: IdempotencyKeyHeader,
    service: Annotated[HeadcountService, Depends(get_headcount_service)],
    idempotency: Annotated[IdempotencyRepository, Depends(get_idempotency_repository)],
) -> HeadcountReservationResponse:
    """Optional and off by default (JA-Q-14): refused unless the organization enables it."""
    key = require_idempotency_key(idempotency_key, endpoint="Creating a reservation")
    guard = idempotency_guard(
        repository=idempotency,
        context=context,
        key=key,
        method="POST",
        path=str(request.url.path),
        payload=payload.model_dump(mode="json"),
    )
    replayed = guard.replay()
    if replayed is not None:
        return HeadcountReservationResponse.model_validate(replayed)

    with unit_of_work(db):
        guard.claim()
        reservation = service.create_reservation(
            context, position_id=position_id, values=payload.model_dump()
        )
        result = _reservation_response(reservation)
        guard.complete(status_code=status.HTTP_201_CREATED, body=result.model_dump(mode="json"))
    return _finalise(response, reservation, result)


@router.post(
    "/positions/{position_id}/headcount-reservations/{reservation_id}/release",
    response_model=HeadcountReservationResponse,
    summary="Release a reservation and restore vacancy",
)
def release_reservation(
    position_id: uuid.UUID,
    reservation_id: uuid.UUID,
    payload: ReasonRequest,
    response: Response,
    context: CurrentOrg,
    if_match: IfMatch,
    db: DbSession,
    service: Annotated[HeadcountService, Depends(get_headcount_service)],
) -> HeadcountReservationResponse:
    with unit_of_work(db):
        reservation = service.release_reservation(
            context,
            position_id=position_id,
            reservation_id=reservation_id,
            reason=payload.reason,
            if_match=if_match,
        )
        result = _reservation_response(reservation)
    return _finalise(response, reservation, result)


# ------------------------------------------------------------------- plumbing


def _decode_cursor(
    codec: CursorCodec, cursor: str | None, spec: SortSpec
) -> tuple[Any, uuid.UUID | None, SortDirection]:
    if cursor is None:
        return None, None, spec.direction
    payload = codec.decode(cursor)
    if payload.sort_field != spec.field:
        raise ValidationFailed("The cursor was issued for a different sort.")
    return payload.sort_value, payload.last_id, payload.direction


def _plan_sort_column(field: str) -> str:
    return {
        "plan_code": "plan_code_normalized",
        "name": "name",
        "effective_from": "effective_from",
        "created_at": "created_at",
    }[field]


__all__ = ["router"]
