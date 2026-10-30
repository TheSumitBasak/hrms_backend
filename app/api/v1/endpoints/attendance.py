"""Attendance: shifts, policies, assignments, punches, records, corrections (addendum 5, 11).

Handlers parse, delegate and serialise; every rule lives in the attendance services.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, Response, status

from app.api.deps import (
    CurrentOrg,
    DbSession,
    IdempotencyKeyHeader,
    IfMatch,
    get_attendance_config_service,
    get_attendance_service,
    get_idempotency_repository,
    get_regularization_service,
)
from app.api.idempotency import idempotency_guard, require_idempotency_key
from app.core.context import OrgContext
from app.core.errors import EmsError, ModuleNotEnabled, ValidationFailed
from app.core.permissions import Permission
from app.db.session import unit_of_work
from app.models.attendance import (
    AttendancePolicy,
    AttendancePolicyVersion,
    AttendancePunch,
    AttendanceRecord,
    Shift,
)
from app.repositories.leave import BackgroundJobRunRepository
from app.repositories.platform import IdempotencyRepository
from app.schemas.attendance import (
    ArchiveRequest,
    AssignmentRequest,
    AssignmentResponse,
    CorrectionRequest,
    DayCloseRequest,
    FinalizeRequest,
    FinalizeResponse,
    FinalizeSkip,
    ImportOutcome,
    ImportRequest,
    ImportResponse,
    ManualPunchRequest,
    PolicyCreateRequest,
    PolicyResponse,
    PolicyUpdateRequest,
    PolicyVersionFields,
    PolicyVersionResponse,
    PunchResponse,
    PunchResult,
    RecordDetail,
    RecordResponse,
    RegularizationActionOut,
    RegularizationApproveBody,
    RegularizationItemOut,
    RegularizationOnBehalfBody,
    RegularizationReassignBody,
    RegularizationRejectBody,
    RegularizationRequestBody,
    RegularizationResponse,
    RegularizationWithdrawBody,
    ReopenRequest,
    ResolveRequest,
    RevisionResponse,
    ScheduleResponse,
    SelfPunchRequest,
    ShiftCreateRequest,
    ShiftResponse,
    ShiftUpdateRequest,
)
from app.schemas.common import Page, etag_for
from app.schemas.leave import JobRunResponse
from app.services.attendance import AttendanceService, PunchOutcome
from app.services.attendance_config import AttendanceConfigService
from app.services.attendance_jobs import MANUAL_WINDOW_DAYS, AttendanceJobs
from app.services.attendance_regularization import RegularizationService, RequestView

router = APIRouter(prefix="/orgs/{org_id}", tags=["Attendance"])

Config = Annotated[AttendanceConfigService, Depends(get_attendance_config_service)]
Service = Annotated[AttendanceService, Depends(get_attendance_service)]
IdempotencyStore = Annotated[IdempotencyRepository, Depends(get_idempotency_repository)]


def _etag(response: Response, row: Any) -> None:
    response.headers["ETag"] = etag_for(row.row_version)


def _shift(row: Shift) -> ShiftResponse:
    return ShiftResponse.model_validate(row)


def _version(row: AttendancePolicyVersion) -> PolicyVersionResponse:
    return PolicyVersionResponse.model_validate(row)


def _policy(row: AttendancePolicy, versions: list[AttendancePolicyVersion]) -> PolicyResponse:
    result = PolicyResponse.model_validate(row)
    result.versions = [_version(v) for v in versions]
    return result


def _punch(row: AttendancePunch) -> PunchResponse:
    return PunchResponse.model_validate(row)


def _record(row: AttendanceRecord, context: OrgContext) -> RecordResponse:
    result = RecordResponse.model_validate(row)
    result.revision_count = row.revision_no
    # 10.5: HR remarks are for attendance.correct holders only, never SELF.
    result.remarks = row.remarks if context.has(Permission.ATTENDANCE_CORRECT) else None
    return result


# Shifts.


@router.get("/shifts", response_model=Page[ShiftResponse])
def list_shifts(
    context: CurrentOrg,
    service: Config,
    status_filter: Annotated[
        str | None, Query(alias="status", pattern="^(ACTIVE|INACTIVE)$")
    ] = None,
) -> Page[ShiftResponse]:
    return Page[ShiftResponse](
        items=[_shift(r) for r in service.list_shifts(context, status=status_filter)]
    )


@router.post("/shifts", response_model=ShiftResponse, status_code=status.HTTP_201_CREATED)
def create_shift(
    payload: ShiftCreateRequest,
    response: Response,
    context: CurrentOrg,
    db: DbSession,
    service: Config,
) -> ShiftResponse:
    with unit_of_work(db):
        row = service.create_shift(context, values=payload.model_dump())
        result = _shift(row)
    _etag(response, row)
    result.row_version = row.row_version
    return result


@router.get("/shifts/{shift_id}", response_model=ShiftResponse)
def read_shift(
    shift_id: uuid.UUID, response: Response, context: CurrentOrg, service: Config
) -> ShiftResponse:
    row = service.get_shift(context, shift_id)
    _etag(response, row)
    return _shift(row)


@router.patch("/shifts/{shift_id}", response_model=ShiftResponse)
def update_shift(
    shift_id: uuid.UUID,
    payload: ShiftUpdateRequest,
    response: Response,
    context: CurrentOrg,
    if_match: IfMatch,
    db: DbSession,
    service: Config,
) -> ShiftResponse:
    with unit_of_work(db):
        row = service.update_shift(
            context,
            shift_id=shift_id,
            changes=payload.model_dump(exclude_unset=True, exclude_none=True),
            if_match=if_match,
        )
        result = _shift(row)
    _etag(response, row)
    result.row_version = row.row_version
    return result


# Policies.


@router.get("/attendance-policies", response_model=Page[PolicyResponse])
def list_policies(
    context: CurrentOrg,
    service: Config,
    status_filter: Annotated[
        str | None, Query(alias="status", pattern="^(ACTIVE|INACTIVE)$")
    ] = None,
) -> Page[PolicyResponse]:
    rows, versions = service.list_policies(context, status=status_filter)
    return Page[PolicyResponse](items=[_policy(r, versions[r.id]) for r in rows])


@router.post(
    "/attendance-policies", response_model=PolicyResponse, status_code=status.HTTP_201_CREATED
)
def create_policy(
    payload: PolicyCreateRequest,
    response: Response,
    context: CurrentOrg,
    db: DbSession,
    service: Config,
) -> PolicyResponse:
    with unit_of_work(db):
        policy, versions = service.create_policy(
            context,
            values=payload.model_dump(exclude={"version"}),
            version=payload.version.model_dump(exclude_none=True),
        )
        result = _policy(policy, versions)
    _etag(response, policy)
    result.row_version = policy.row_version
    return result


@router.post("/attendance-policies/resolve", response_model=ScheduleResponse)
def resolve_schedule(
    payload: ResolveRequest, context: CurrentOrg, service: Config
) -> ScheduleResponse:
    schedule = service.resolve(context, employee_id=payload.employee_id, on=payload.on)
    return ScheduleResponse(
        day_type=schedule.day_type,
        holiday_id=schedule.holiday.id if schedule.holiday else None,
        holiday_calendar_id=schedule.calendar_id,
        weekly_off_pattern_id=schedule.pattern_id,
        policy_version_id=schedule.policy_version.id if schedule.policy_version else None,
        shift_id=schedule.shift.id if schedule.shift else None,
        shift_assignment_id=schedule.assignment.id if schedule.assignment else None,
        flexible=schedule.shift is None,
    )


@router.get("/attendance-policies/{policy_id}", response_model=PolicyResponse)
def read_policy(
    policy_id: uuid.UUID, response: Response, context: CurrentOrg, service: Config
) -> PolicyResponse:
    policy, versions = service.get_policy(context, policy_id)
    _etag(response, policy)
    return _policy(policy, versions)


@router.patch("/attendance-policies/{policy_id}", response_model=PolicyResponse)
def update_policy(
    policy_id: uuid.UUID,
    payload: PolicyUpdateRequest,
    response: Response,
    context: CurrentOrg,
    if_match: IfMatch,
    db: DbSession,
    service: Config,
) -> PolicyResponse:
    with unit_of_work(db):
        policy = service.update_policy(
            context,
            policy_id=policy_id,
            changes=payload.model_dump(exclude_unset=True, exclude_none=True),
            if_match=if_match,
        )
        _, versions = service.get_policy(context, policy_id)
        result = _policy(policy, versions)
    _etag(response, policy)
    result.row_version = policy.row_version
    return result


@router.post(
    "/attendance-policies/{policy_id}/versions",
    response_model=PolicyVersionResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_version(
    policy_id: uuid.UUID,
    payload: PolicyVersionFields,
    response: Response,
    context: CurrentOrg,
    db: DbSession,
    service: Config,
) -> PolicyVersionResponse:
    with unit_of_work(db):
        row = service.create_version(
            context, policy_id=policy_id, fields=payload.model_dump(exclude_none=True)
        )
        result = _version(row)
    _etag(response, row)
    result.row_version = row.row_version
    return result


@router.patch(
    "/attendance-policies/{policy_id}/versions/{version_id}", response_model=PolicyVersionResponse
)
def update_version(
    policy_id: uuid.UUID,
    version_id: uuid.UUID,
    payload: PolicyVersionFields,
    response: Response,
    context: CurrentOrg,
    if_match: IfMatch,
    db: DbSession,
    service: Config,
) -> PolicyVersionResponse:
    with unit_of_work(db):
        row = service.update_version(
            context,
            policy_id=policy_id,
            version_id=version_id,
            changes=payload.model_dump(exclude_unset=True),
            if_match=if_match,
        )
        result = _version(row)
    _etag(response, row)
    result.row_version = row.row_version
    return result


@router.post(
    "/attendance-policies/{policy_id}/versions/{version_id}/publish",
    response_model=PolicyVersionResponse,
)
def publish_version(
    policy_id: uuid.UUID,
    version_id: uuid.UUID,
    response: Response,
    context: CurrentOrg,
    if_match: IfMatch,
    db: DbSession,
    service: Config,
) -> PolicyVersionResponse:
    with unit_of_work(db):
        row = service.publish_version(
            context, policy_id=policy_id, version_id=version_id, if_match=if_match
        )
        result = _version(row)
    _etag(response, row)
    result.row_version = row.row_version
    return result


@router.post(
    "/attendance-policies/{policy_id}/versions/{version_id}/archive",
    response_model=PolicyVersionResponse,
)
def archive_version(
    policy_id: uuid.UUID,
    version_id: uuid.UUID,
    payload: ArchiveRequest,
    response: Response,
    context: CurrentOrg,
    if_match: IfMatch,
    db: DbSession,
    service: Config,
) -> PolicyVersionResponse:
    with unit_of_work(db):
        row = service.archive_version(
            context,
            policy_id=policy_id,
            version_id=version_id,
            reason=payload.reason,
            if_match=if_match,
        )
        result = _version(row)
    _etag(response, row)
    result.row_version = row.row_version
    return result


# Shift assignments.


@router.get("/employees/{employee_id}/shift-assignments", response_model=Page[AssignmentResponse])
def list_assignments(
    employee_id: uuid.UUID, context: CurrentOrg, service: Config
) -> Page[AssignmentResponse]:
    rows = service.list_assignments(context, employee_id)
    return Page[AssignmentResponse](items=[AssignmentResponse.model_validate(r) for r in rows])


@router.post(
    "/employees/{employee_id}/shift-assignments",
    response_model=AssignmentResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_assignment(
    employee_id: uuid.UUID,
    payload: AssignmentRequest,
    response: Response,
    context: CurrentOrg,
    db: DbSession,
    service: Config,
) -> AssignmentResponse:
    with unit_of_work(db):
        row, adjustment = service.create_assignment(
            context,
            employee_id=employee_id,
            values=payload.model_dump(exclude={"acknowledged_warnings"}),
            acknowledged=payload.acknowledged_warnings,
        )
        result = AssignmentResponse.model_validate(row)
        result.boundary_adjustment = adjustment
    _etag(response, row)
    result.row_version = row.row_version
    return result


# Punches.


def _punch_result(outcome: PunchOutcome, context: OrgContext) -> PunchResult:
    return PunchResult(
        punch=_punch(outcome.punch),
        outcome=outcome.outcome,
        duplicate_of=outcome.duplicate_of,
        record=_record(outcome.record, context),
    )


def _idempotent_punch(
    *,
    request: Request,
    context: OrgContext,
    db: Any,
    key: uuid.UUID | None,
    idempotency: IdempotencyRepository,
    payload: dict[str, Any],
    run: Any,
) -> PunchResult:
    guard = None
    if key is not None:
        guard = idempotency_guard(
            repository=idempotency,
            context=context,
            key=key,
            method="POST",
            path=str(request.url.path),
            payload=payload,
        )
        replayed = guard.replay()
        if replayed is not None:
            return PunchResult.model_validate(replayed)
    with unit_of_work(db):
        if guard is not None:
            guard.claim()
        result = _punch_result(run(), context)
        if guard is not None:
            guard.complete(status_code=status.HTTP_200_OK, body=result.model_dump(mode="json"))
    return result


@router.post("/me/attendance/punches", response_model=PunchResult)
def self_punch(
    payload: SelfPunchRequest,
    request: Request,
    context: CurrentOrg,
    db: DbSession,
    idempotency_key: IdempotencyKeyHeader,
    service: Service,
    idempotency: IdempotencyStore,
) -> PunchResult:
    return _idempotent_punch(
        request=request,
        context=context,
        db=db,
        key=idempotency_key,
        idempotency=idempotency,
        payload=payload.model_dump(mode="json"),
        run=lambda: service.self_punch(
            context, punch_type=payload.punch_type, work_arrangement=payload.work_arrangement
        ),
    )


@router.post("/employees/{employee_id}/attendance/punches", response_model=PunchResult)
def manual_punch(
    employee_id: uuid.UUID,
    payload: ManualPunchRequest,
    request: Request,
    context: CurrentOrg,
    db: DbSession,
    idempotency_key: IdempotencyKeyHeader,
    service: Service,
    idempotency: IdempotencyStore,
) -> PunchResult:
    return _idempotent_punch(
        request=request,
        context=context,
        db=db,
        key=idempotency_key,
        idempotency=idempotency,
        payload=payload.model_dump(mode="json"),
        run=lambda: service.manual_punch(
            context,
            employee_id=employee_id,
            punch_at=payload.punch_at,
            punch_type=payload.punch_type,
            work_arrangement=payload.work_arrangement,
            remarks=payload.remarks,
        ),
    )


@router.post("/attendance/punch-imports", response_model=ImportResponse)
def import_punches(
    payload: ImportRequest, context: CurrentOrg, db: DbSession, service: Service
) -> ImportResponse:
    """5.16: each item runs in its own transaction; one bad item never blocks the batch."""
    service.require_import(context)
    results: list[ImportOutcome] = []
    for item in payload.items:
        try:
            with unit_of_work(db):
                outcome = service.integration_punch(
                    context,
                    employee_id=item.employee_id,
                    employee_code=item.employee_code,
                    punch_at=item.punch_at,
                    punch_type=item.punch_type,
                    integration_source_code=item.integration_source_code,
                    external_event_id=item.external_event_id,
                )
                results.append(
                    ImportOutcome(
                        external_event_id=item.external_event_id,
                        outcome=outcome.outcome,
                        punch_id=outcome.punch.id,
                    )
                )
        except EmsError as error:
            results.append(
                ImportOutcome(
                    external_event_id=item.external_event_id,
                    outcome="REJECTED",
                    error_code=error.code.value,
                )
            )
    return ImportResponse(results=results)


# Records.


@router.get("/employees/{employee_id}/attendance/punches", response_model=Page[PunchResponse])
def list_punches(
    employee_id: uuid.UUID,
    context: CurrentOrg,
    service: Service,
    start: Annotated[date, Query(alias="from")],
    end: Annotated[date, Query(alias="to")],
    include_voided: bool = False,
) -> Page[PunchResponse]:
    rows = service.list_punches(
        context, employee_id=employee_id, start=start, end=end, include_voided=include_voided
    )
    return Page[PunchResponse](items=[_punch(r) for r in rows])


@router.get("/employees/{employee_id}/attendance", response_model=Page[RecordResponse])
def list_records(
    employee_id: uuid.UUID,
    context: CurrentOrg,
    service: Service,
    start: Annotated[date, Query(alias="from")],
    end: Annotated[date, Query(alias="to")],
) -> Page[RecordResponse]:
    rows = service.list_records(context, employee_id=employee_id, start=start, end=end)
    return Page[RecordResponse](items=[_record(r, context) for r in rows])


@router.get("/me/attendance", response_model=Page[RecordResponse])
def my_records(
    context: CurrentOrg,
    service: Service,
    start: Annotated[date, Query(alias="from")],
    end: Annotated[date, Query(alias="to")],
) -> Page[RecordResponse]:
    rows = service.list_records(
        context, employee_id=service.own_employee_id(context), start=start, end=end
    )
    return Page[RecordResponse](items=[_record(r, context) for r in rows])


def _detail(
    record: AttendanceRecord,
    punches: list[AttendancePunch],
    context: OrgContext,
    response: Response,
) -> RecordDetail:
    base = _record(record, context).model_dump()
    result = RecordDetail(**base, punches=[_punch(p) for p in punches])
    _etag(response, record)
    result.row_version = record.row_version
    return result


@router.get("/employees/{employee_id}/attendance/{day}", response_model=RecordDetail)
def read_record(
    employee_id: uuid.UUID, day: date, response: Response, context: CurrentOrg, service: Service
) -> RecordDetail:
    record, punches, _ = service.get_record(context, employee_id, day)
    return _detail(record, punches, context, response)


@router.get("/me/attendance/{day}", response_model=RecordDetail)
def my_record(day: date, response: Response, context: CurrentOrg, service: Service) -> RecordDetail:
    record, punches, _ = service.get_record(context, service.own_employee_id(context), day)
    return _detail(record, punches, context, response)


@router.get(
    "/employees/{employee_id}/attendance/{day}/revisions", response_model=Page[RevisionResponse]
)
def read_revisions(
    employee_id: uuid.UUID, day: date, context: CurrentOrg, service: Service
) -> Page[RevisionResponse]:
    rows = service.revisions(context, employee_id, day)
    return Page[RevisionResponse](items=[RevisionResponse.model_validate(r) for r in rows])


@router.post("/attendance/finalize", response_model=FinalizeResponse)
def finalize(
    payload: FinalizeRequest, context: CurrentOrg, db: DbSession, service: Service
) -> FinalizeResponse:
    with unit_of_work(db):
        result = service.finalize(
            context, employee_id=payload.employee_id, start=payload.start, end=payload.end
        )
        return FinalizeResponse(
            finalized_count=result.finalized,
            skipped=[
                FinalizeSkip(record_id=uuid.UUID(s["record_id"]), reason=s["reason"])
                for s in result.skipped
            ],
        )


@router.post("/employees/{employee_id}/attendance/{day}/reopen", response_model=RecordResponse)
def reopen(
    employee_id: uuid.UUID,
    day: date,
    payload: ReopenRequest,
    response: Response,
    context: CurrentOrg,
    if_match: IfMatch,
    db: DbSession,
    service: Service,
) -> RecordResponse:
    with unit_of_work(db):
        record = service.reopen(
            context, employee_id=employee_id, day=day, reason=payload.reason, if_match=if_match
        )
        result = _record(record, context)
    _etag(response, record)
    result.row_version = record.row_version
    return result


@router.post("/employees/{employee_id}/attendance/{day}/corrections", response_model=RecordResponse)
def correct(
    employee_id: uuid.UUID,
    day: date,
    payload: CorrectionRequest,
    response: Response,
    context: CurrentOrg,
    if_match: IfMatch,
    db: DbSession,
    service: Service,
) -> RecordResponse:
    operations = [op.model_dump(exclude_none=True) for op in payload.operations]
    with unit_of_work(db):
        record = service.correct(
            context,
            employee_id=employee_id,
            day=day,
            operations=operations,
            reason=payload.reason,
            if_match=if_match,
        )
        result = _record(record, context)
    _etag(response, record)
    result.row_version = record.row_version
    return result


@router.post(
    "/attendance/day-close-runs", response_model=JobRunResponse, status_code=status.HTTP_201_CREATED
)
def trigger_day_close(
    payload: DayCloseRequest,
    context: CurrentOrg,
    db: DbSession,
    idempotency_key: IdempotencyKeyHeader,
) -> JobRunResponse:
    """5.16 / 14.1: a manual day close for one date within the last 31 days (attendance.correct)."""
    context.require(Permission.ATTENDANCE_CORRECT)
    key = require_idempotency_key(idempotency_key, endpoint="Triggering a day close")
    jobs = AttendanceJobs(db, context.organization_id)
    if not jobs.settings.enable_attendance_module:
        raise ModuleNotEnabled()
    today = jobs.today()
    low = today - timedelta(days=MANUAL_WINDOW_DAYS)
    if not low <= payload.business_date <= today - timedelta(days=1):
        raise ValidationFailed("business_date must be within the last 31 days and before today.")
    run_key = f"MANUAL:{key}"
    existing = BackgroundJobRunRepository(db, context.organization_id).by_run_key(
        "ATTENDANCE_DAY_CLOSE", run_key
    )
    if existing is not None:
        return JobRunResponse.model_validate(existing)
    run = jobs.run_day_close(
        [payload.business_date], run_key=run_key, trigger="MANUAL", requested_by=context.user_id
    )
    return JobRunResponse.model_validate(run)


# Regularization (5.14).

Regularizations = Annotated[RegularizationService, Depends(get_regularization_service)]


def _regularization(view: RequestView) -> RegularizationResponse:
    result = RegularizationResponse.model_validate(view.request)
    result.supporting_document_attached = view.request.supporting_document_id is not None
    result.items = [RegularizationItemOut.model_validate(i) for i in view.items]
    result.actions = [RegularizationActionOut.model_validate(a) for a in view.actions]
    return result


def _stamp_request(
    response: Response, view: RequestView, result: RegularizationResponse
) -> RegularizationResponse:
    """Publish the committed ``row_version`` (read after commit) as ETag and body field."""
    _etag(response, view.request)
    result.row_version = view.request.row_version
    return result


def _submit_regularization(
    *,
    payload: RegularizationRequestBody,
    employee_id: uuid.UUID | None,
    on_behalf_reason: str | None,
    response: Response,
    context: OrgContext,
    db: Any,
    service: RegularizationService,
) -> RegularizationResponse:
    items = [
        {k: v for k, v in item.model_dump().items() if v is not None} for item in payload.items
    ]
    with unit_of_work(db):
        view = service.submit(
            context,
            employee_id=employee_id,
            on_behalf=employee_id is not None,
            on_behalf_reason=on_behalf_reason,
            day=payload.business_date,
            request_type=payload.request_type,
            reason=payload.reason,
            items=items,
            supporting_document_id=payload.supporting_document_id,
            resubmission_of=payload.resubmission_of_request_id,
        )
        result = _regularization(view)
    response.status_code = status.HTTP_201_CREATED
    return _stamp_request(response, view, result)


@router.post(
    "/me/attendance/regularizations",
    response_model=RegularizationResponse,
    status_code=status.HTTP_201_CREATED,
)
def submit_own_regularization(
    payload: RegularizationRequestBody,
    response: Response,
    context: CurrentOrg,
    db: DbSession,
    service: Regularizations,
) -> RegularizationResponse:
    return _submit_regularization(
        payload=payload,
        employee_id=None,
        on_behalf_reason=None,
        response=response,
        context=context,
        db=db,
        service=service,
    )


@router.post(
    "/employees/{employee_id}/attendance/regularizations",
    response_model=RegularizationResponse,
    status_code=status.HTTP_201_CREATED,
)
def submit_regularization_on_behalf(
    employee_id: uuid.UUID,
    payload: RegularizationOnBehalfBody,
    response: Response,
    context: CurrentOrg,
    db: DbSession,
    service: Regularizations,
) -> RegularizationResponse:
    return _submit_regularization(
        payload=payload,
        employee_id=employee_id,
        on_behalf_reason=payload.on_behalf_reason,
        response=response,
        context=context,
        db=db,
        service=service,
    )


@router.get("/attendance/regularizations", response_model=Page[RegularizationResponse])
def list_regularizations(
    context: CurrentOrg,
    service: Regularizations,
    status_filter: Annotated[list[str] | None, Query(alias="status")] = None,
    employee_id: uuid.UUID | None = None,
    approver: Annotated[str | None, Query(pattern="^me$")] = None,
    hr_queue: bool = False,
) -> Page[RegularizationResponse]:
    rows = service.list_requests(
        context,
        employee_id=employee_id,
        statuses=status_filter,
        approver_me=approver == "me",
        hr_queue=hr_queue,
    )
    return Page[RegularizationResponse](
        items=[RegularizationResponse.model_validate(r) for r in rows]
    )


@router.get("/me/attendance/regularizations", response_model=Page[RegularizationResponse])
def list_own_regularizations(
    context: CurrentOrg, service: Regularizations
) -> Page[RegularizationResponse]:
    rows = service.list_requests(
        context, employee_id=None, statuses=None, approver_me=False, hr_queue=False, own=True
    )
    return Page[RegularizationResponse](
        items=[RegularizationResponse.model_validate(r) for r in rows]
    )


@router.get("/attendance/regularizations/{request_id}", response_model=RegularizationResponse)
def read_regularization(
    request_id: uuid.UUID, response: Response, context: CurrentOrg, service: Regularizations
) -> RegularizationResponse:
    view = service.get(context, request_id)
    return _stamp_request(response, view, _regularization(view))


@router.post(
    "/attendance/regularizations/{request_id}/approve", response_model=RegularizationResponse
)
def approve_regularization(
    request_id: uuid.UUID,
    payload: RegularizationApproveBody,
    response: Response,
    context: CurrentOrg,
    if_match: IfMatch,
    db: DbSession,
    service: Regularizations,
) -> RegularizationResponse:
    with unit_of_work(db):
        view = service.approve(context, request_id, comment=payload.comment, if_match=if_match)
        result = _regularization(view)
    return _stamp_request(response, view, result)


@router.post(
    "/attendance/regularizations/{request_id}/reject", response_model=RegularizationResponse
)
def reject_regularization(
    request_id: uuid.UUID,
    payload: RegularizationRejectBody,
    response: Response,
    context: CurrentOrg,
    if_match: IfMatch,
    db: DbSession,
    service: Regularizations,
) -> RegularizationResponse:
    with unit_of_work(db):
        view = service.reject(
            context, request_id, decision_comment=payload.decision_comment, if_match=if_match
        )
        result = _regularization(view)
    return _stamp_request(response, view, result)


@router.post(
    "/attendance/regularizations/{request_id}/withdraw", response_model=RegularizationResponse
)
@router.post(
    "/me/attendance/regularizations/{request_id}/withdraw", response_model=RegularizationResponse
)
def withdraw_regularization(
    request_id: uuid.UUID,
    payload: RegularizationWithdrawBody,
    response: Response,
    context: CurrentOrg,
    if_match: IfMatch,
    db: DbSession,
    service: Regularizations,
) -> RegularizationResponse:
    with unit_of_work(db):
        view = service.withdraw(context, request_id, reason=payload.reason, if_match=if_match)
        result = _regularization(view)
    return _stamp_request(response, view, result)


@router.post(
    "/attendance/regularizations/{request_id}/reassign", response_model=RegularizationResponse
)
def reassign_regularization(
    request_id: uuid.UUID,
    payload: RegularizationReassignBody,
    response: Response,
    context: CurrentOrg,
    if_match: IfMatch,
    db: DbSession,
    service: Regularizations,
) -> RegularizationResponse:
    with unit_of_work(db):
        view = service.reassign(
            context,
            request_id,
            to_user_id=payload.to_user_id,
            to_hr_queue=payload.to_hr_queue,
            reason=payload.reason,
            if_match=if_match,
        )
        result = _regularization(view)
    return _stamp_request(response, view, result)
