"""Leave management: types, policies, ledger, applications and self-service (addendum §6, §11).

Handlers parse, delegate and serialise; every rule lives in the leave services.
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
    get_idempotency_repository,
    get_leave_application_service,
    get_leave_config_service,
    get_leave_ledger_service,
)
from app.api.idempotency import idempotency_guard, require_idempotency_key
from app.core.context import OrgContext
from app.core.errors import ModuleNotEnabled, ValidationFailed
from app.core.permissions import Permission
from app.db.session import unit_of_work
from app.models.leave import (
    LeaveApplication,
    LeaveBalance,
    LeavePolicy,
    LeavePolicyVersion,
    LeaveTransaction,
    LeaveType,
)
from app.repositories.leave import BackgroundJobRunRepository
from app.repositories.platform import IdempotencyRepository
from app.schemas.common import MAX_PAGE_SIZE, Page, etag_for
from app.schemas.leave import (
    ActionResponse,
    AdminTransactionRequest,
    ApplicationRequest,
    ApplicationResponse,
    ApproveRequest,
    ArchiveVersionRequest,
    BalanceResponse,
    BalancesResponse,
    CancelRequest,
    JobRunRequest,
    JobRunResponse,
    LeaveTypeCreateRequest,
    LeaveTypeResponse,
    LeaveTypeUpdateRequest,
    OnBehalfApplicationRequest,
    PolicyCreateRequest,
    PolicyResolveRequest,
    PolicyResponse,
    PolicyUpdateRequest,
    PolicyVersionFields,
    PolicyVersionResponse,
    PreviewRequest,
    PreviewResponse,
    ReassignRequest,
    RecalculateRequest,
    RejectRequest,
    ReverseTransactionRequest,
    SlotResponse,
    StatusChangeRequest,
    TransactionResponse,
    TransactionWithBalance,
    WithdrawRequest,
)
from app.services.leave_applications import (
    ApplicationView,
    Evaluation,
    LeaveApplicationService,
)
from app.services.leave_config import LeaveConfigService
from app.services.leave_jobs import LeaveJobs
from app.services.leave_ledger import LeaveLedgerService

router = APIRouter(prefix="/orgs/{org_id}", tags=["Leave"])

Config = Annotated[LeaveConfigService, Depends(get_leave_config_service)]
Ledger = Annotated[LeaveLedgerService, Depends(get_leave_ledger_service)]
Applications = Annotated[LeaveApplicationService, Depends(get_leave_application_service)]
IdempotencyStore = Annotated[IdempotencyRepository, Depends(get_idempotency_repository)]
PageLimit = Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)]


def _etag(response: Response, row: Any) -> None:
    response.headers["ETag"] = etag_for(row.row_version)


# ----------------------------------------------------------------------------- serialisers


def _type(row: LeaveType) -> LeaveTypeResponse:
    return LeaveTypeResponse.model_validate(row)


def _version(row: LeavePolicyVersion) -> PolicyVersionResponse:
    return PolicyVersionResponse.model_validate(row)


def _policy(row: LeavePolicy, versions: list[LeavePolicyVersion]) -> PolicyResponse:
    base = PolicyResponse.model_validate(row)
    base.versions = [_version(v) for v in versions]
    return base


def _balance(
    row: LeaveBalance, leave_type: LeaveType, as_of: Decimal | None = None
) -> BalanceResponse:
    return BalanceResponse(
        leave_type_id=row.leave_type_id,
        leave_type_code=leave_type.leave_type_code,
        leave_type_name=leave_type.leave_type_name,
        leave_year_start=row.leave_year_start,
        leave_year_end=row.leave_year_end,
        credited_days=row.credited_days,
        debited_days=row.debited_days,
        posted_balance_days=row.posted_balance_days,
        reserved_days=row.reserved_days,
        available_days=row.available_days,
        closure_status=row.closure_status,
        posted_balance_as_of=as_of,
    )


def _transaction(row: LeaveTransaction, *, show_reason: bool) -> TransactionResponse:
    result = TransactionResponse.model_validate(row)
    if not show_reason:
        result.reason = None
    return result


def _application(
    view: ApplicationView,
    *,
    show_reason: bool,
    leave_type: LeaveType | None = None,
    restored: Decimal | None = None,
) -> ApplicationResponse:
    row = view.application
    result = ApplicationResponse.model_validate(row)
    result.reason = row.reason if show_reason else None
    result.supporting_document_attached = row.supporting_document_id is not None
    result.slots = [SlotResponse.model_validate(s) for s in view.slots]
    result.actions = [ActionResponse.model_validate(a) for a in view.actions]
    if view.balance is not None and leave_type is not None:
        result.balance = _balance(view.balance, leave_type)
    result.restored_days = restored
    return result


def _preview(evaluation: Evaluation) -> PreviewResponse:
    after = (
        None
        if evaluation.available_before is None
        else evaluation.available_before - evaluation.requested_days
    )
    return PreviewResponse(
        leave_type_id=evaluation.leave_type.id,
        leave_year_start=evaluation.year_start,
        requested_days=evaluation.requested_days,
        slots=[
            SlotResponse(
                slot_date=s.slot_date, half=s.half.value, day_type=s.day_type, counted=s.counted
            )
            for s in evaluation.slots
        ],
        available_before=evaluation.available_before,
        available_after=after,
        policy_version_id=evaluation.version.id,
        warnings=evaluation.warnings,
    )


# ----------------------------------------------------------------------------- leave types


@router.get("/leave-types", response_model=Page[LeaveTypeResponse])
def list_leave_types(
    context: CurrentOrg,
    service: Config,
    status_filter: Annotated[
        str | None, Query(alias="status", pattern="^(ACTIVE|INACTIVE)$")
    ] = None,
) -> Page[LeaveTypeResponse]:
    rows = service.list_types(context, status=status_filter)
    return Page[LeaveTypeResponse](items=[_type(r) for r in rows])


@router.post("/leave-types", response_model=LeaveTypeResponse, status_code=status.HTTP_201_CREATED)
def create_leave_type(
    payload: LeaveTypeCreateRequest,
    response: Response,
    context: CurrentOrg,
    db: DbSession,
    service: Config,
) -> LeaveTypeResponse:
    with unit_of_work(db):
        row = service.create_type(context, values=payload.model_dump())
        result = _type(row)
    _etag(response, row)
    result.row_version = row.row_version
    return result


@router.get("/leave-types/{type_id}", response_model=LeaveTypeResponse)
def read_leave_type(
    type_id: uuid.UUID, response: Response, context: CurrentOrg, service: Config
) -> LeaveTypeResponse:
    row = service.get_type(context, type_id)
    _etag(response, row)
    return _type(row)


@router.patch("/leave-types/{type_id}", response_model=LeaveTypeResponse)
def update_leave_type(
    type_id: uuid.UUID,
    payload: LeaveTypeUpdateRequest,
    response: Response,
    context: CurrentOrg,
    if_match: IfMatch,
    db: DbSession,
    service: Config,
) -> LeaveTypeResponse:
    with unit_of_work(db):
        row = service.update_type(
            context,
            type_id=type_id,
            changes=payload.model_dump(exclude_unset=True, exclude_none=True),
            if_match=if_match,
        )
        result = _type(row)
    _etag(response, row)
    result.row_version = row.row_version
    return result


@router.post("/leave-types/{type_id}/deactivate", response_model=LeaveTypeResponse)
@router.post("/leave-types/{type_id}/reactivate", response_model=LeaveTypeResponse)
def change_leave_type_status(
    type_id: uuid.UUID,
    payload: StatusChangeRequest,
    request: Request,
    response: Response,
    context: CurrentOrg,
    db: DbSession,
    service: Config,
) -> LeaveTypeResponse:
    target = "INACTIVE" if request.url.path.endswith("/deactivate") else "ACTIVE"
    with unit_of_work(db):
        row = service.set_type_status(
            context, type_id=type_id, status=target, reason=payload.reason
        )
        result = _type(row)
    _etag(response, row)
    result.row_version = row.row_version
    return result


# ----------------------------------------------------------------------------- policies


@router.get("/leave-policies", response_model=Page[PolicyResponse])
def list_leave_policies(
    context: CurrentOrg,
    service: Config,
    leave_type_id: uuid.UUID | None = None,
    status_filter: Annotated[
        str | None, Query(alias="status", pattern="^(ACTIVE|INACTIVE)$")
    ] = None,
) -> Page[PolicyResponse]:
    rows, versions = service.list_policies(
        context, leave_type_id=leave_type_id, status=status_filter
    )
    return Page[PolicyResponse](items=[_policy(r, versions[r.id]) for r in rows])


@router.post("/leave-policies", response_model=PolicyResponse, status_code=status.HTTP_201_CREATED)
def create_leave_policy(
    payload: PolicyCreateRequest,
    response: Response,
    context: CurrentOrg,
    db: DbSession,
    service: Config,
) -> PolicyResponse:
    values = payload.model_dump(exclude={"version"})
    with unit_of_work(db):
        policy, versions = service.create_policy(
            context, values=values, version=payload.version.model_dump(exclude_none=True)
        )
        result = _policy(policy, versions)
    _etag(response, policy)
    result.row_version = policy.row_version
    return result


@router.post("/leave-policies/resolve", response_model=PolicyVersionResponse)
def resolve_leave_policy(
    payload: PolicyResolveRequest, context: CurrentOrg, service: Config
) -> PolicyVersionResponse:
    return _version(
        service.resolve(
            context,
            employee_id=payload.employee_id,
            leave_type_id=payload.leave_type_id,
            on=payload.on,
        )
    )


@router.get("/leave-policies/{policy_id}", response_model=PolicyResponse)
def read_leave_policy(
    policy_id: uuid.UUID, response: Response, context: CurrentOrg, service: Config
) -> PolicyResponse:
    policy, versions = service.get_policy(context, policy_id)
    _etag(response, policy)
    return _policy(policy, versions)


@router.patch("/leave-policies/{policy_id}", response_model=PolicyResponse)
def update_leave_policy(
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
    "/leave-policies/{policy_id}/versions",
    response_model=PolicyVersionResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_policy_version(
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
    "/leave-policies/{policy_id}/versions/{version_id}", response_model=PolicyVersionResponse
)
def update_policy_version(
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
    "/leave-policies/{policy_id}/versions/{version_id}/publish",
    response_model=PolicyVersionResponse,
)
def publish_policy_version(
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
    "/leave-policies/{policy_id}/versions/{version_id}/archive",
    response_model=PolicyVersionResponse,
)
def archive_policy_version(
    policy_id: uuid.UUID,
    version_id: uuid.UUID,
    payload: ArchiveVersionRequest,
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


# ----------------------------------------------------------------------------- balances and ledger


def _balances(
    employee_id: uuid.UUID,
    year: date,
    items: list[tuple[LeaveBalance, LeaveType, Decimal | None]],
) -> BalancesResponse:
    return BalancesResponse(
        employee_id=employee_id,
        leave_year_start=year,
        items=[_balance(b, t, posted) for b, t, posted in items],
    )


@router.get("/employees/{employee_id}/leave-balances", response_model=BalancesResponse)
def employee_leave_balances(
    employee_id: uuid.UUID,
    context: CurrentOrg,
    service: Ledger,
    leave_year: date | None = None,
    leave_type_id: uuid.UUID | None = None,
    as_of: date | None = None,
) -> BalancesResponse:
    year, items = service.balances(
        context,
        employee_id=employee_id,
        year_start=leave_year,
        leave_type_id=leave_type_id,
        as_of=as_of,
    )
    return _balances(employee_id, year, items)


@router.get("/me/leave-balances", response_model=BalancesResponse)
def my_leave_balances(
    context: CurrentOrg,
    service: Ledger,
    applications: Applications,
    leave_year: date | None = None,
) -> BalancesResponse:
    employee_id = applications.own_employee_id(context)
    year, items = service.balances(
        context, employee_id=employee_id, year_start=leave_year, leave_type_id=None, as_of=None
    )
    return _balances(employee_id, year, items)


def _transaction_page(
    service: LeaveLedgerService,
    context: Any,
    *,
    employee_id: uuid.UUID,
    leave_type_id: uuid.UUID | None,
    leave_year: date | None,
    transaction_type: list[str] | None,
    limit: int,
    cursor: str | None,
) -> Page[TransactionResponse]:
    rows, next_cursor = service.list_transactions(
        context,
        employee_id=employee_id,
        leave_type_id=leave_type_id,
        leave_year_start=leave_year,
        transaction_types=transaction_type,
        limit=limit,
        cursor=cursor,
    )
    show = service.can_see_reasons(context)
    return Page[TransactionResponse](
        items=[_transaction(r, show_reason=show) for r in rows], next_cursor=next_cursor
    )


@router.get("/employees/{employee_id}/leave-transactions", response_model=Page[TransactionResponse])
def employee_leave_transactions(
    employee_id: uuid.UUID,
    context: CurrentOrg,
    service: Ledger,
    limit: PageLimit = 25,
    cursor: str | None = None,
    leave_type_id: uuid.UUID | None = None,
    leave_year: date | None = None,
    transaction_type: Annotated[list[str] | None, Query()] = None,
) -> Page[TransactionResponse]:
    return _transaction_page(
        service,
        context,
        employee_id=employee_id,
        leave_type_id=leave_type_id,
        leave_year=leave_year,
        transaction_type=transaction_type,
        limit=limit,
        cursor=cursor,
    )


@router.get("/me/leave-transactions", response_model=Page[TransactionResponse])
def my_leave_transactions(
    context: CurrentOrg,
    service: Ledger,
    applications: Applications,
    limit: PageLimit = 25,
    cursor: str | None = None,
    leave_type_id: uuid.UUID | None = None,
    leave_year: date | None = None,
) -> Page[TransactionResponse]:
    return _transaction_page(
        service,
        context,
        employee_id=applications.own_employee_id(context),
        leave_type_id=leave_type_id,
        leave_year=leave_year,
        transaction_type=None,
        limit=limit,
        cursor=cursor,
    )


@router.post(
    "/employees/{employee_id}/leave-transactions",
    response_model=TransactionWithBalance,
    status_code=status.HTTP_201_CREATED,
)
def post_leave_transaction(
    employee_id: uuid.UUID,
    payload: AdminTransactionRequest,
    request: Request,
    context: CurrentOrg,
    db: DbSession,
    idempotency_key: IdempotencyKeyHeader,
    service: Ledger,
    idempotency: IdempotencyStore,
) -> TransactionWithBalance:
    key = require_idempotency_key(idempotency_key, endpoint="Posting a leave transaction")
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
        return TransactionWithBalance.model_validate(replayed)
    values = payload.model_dump(exclude={"acknowledged_warnings"})
    with unit_of_work(db):
        guard.claim()
        row, balance = service.post_admin_transaction(
            context,
            employee_id=employee_id,
            values=values,
            idempotency_key=key,
            acknowledged_warnings=payload.acknowledged_warnings,
        )
        leave_type = service.leave_type(row.leave_type_id)
        result = TransactionWithBalance(
            transaction=_transaction(row, show_reason=True), balance=_balance(balance, leave_type)
        )
        guard.complete(status_code=status.HTTP_201_CREATED, body=result.model_dump(mode="json"))
    return result


@router.post(
    "/employees/{employee_id}/leave-transactions/{transaction_id}/reverse",
    response_model=TransactionWithBalance,
    status_code=status.HTTP_201_CREATED,
)
def reverse_leave_transaction(
    employee_id: uuid.UUID,
    transaction_id: uuid.UUID,
    payload: ReverseTransactionRequest,
    context: CurrentOrg,
    db: DbSession,
    service: Ledger,
) -> TransactionWithBalance:
    # The reversal key REV:{target} is itself the idempotency guarantee (one reversal per target).
    with unit_of_work(db):
        row, balance = service.reverse(
            context, employee_id=employee_id, transaction_id=transaction_id, reason=payload.reason
        )
        leave_type = service.leave_type(row.leave_type_id)
        return TransactionWithBalance(
            transaction=_transaction(row, show_reason=True), balance=_balance(balance, leave_type)
        )


# ----------------------------------------------------------------------------- applications


def _respond(
    view: ApplicationView,
    *,
    applications: LeaveApplicationService,
    show_reason: bool = True,
    restored: Decimal | None = None,
) -> ApplicationResponse:
    return _application(
        view,
        show_reason=show_reason,
        leave_type=applications.leave_type(view.application.leave_type_id),
        restored=restored,
    )


def _stamp(
    response: Response, view: ApplicationView, result: ApplicationResponse
) -> ApplicationResponse:
    """Publish the committed ``row_version`` as ETag and body field (read after commit)."""
    _etag(response, view.application)
    result.row_version = view.application.row_version
    return result


@router.post("/leave-applications/preview", response_model=PreviewResponse)
def preview_leave_application(
    payload: PreviewRequest, context: CurrentOrg, service: Applications
) -> PreviewResponse:
    body = payload.model_dump(exclude={"employee_id", "acknowledged_warnings"})
    return _preview(
        service.preview(
            context,
            employee_id=payload.employee_id,
            body=body,
            acknowledged=payload.acknowledged_warnings,
        )
    )


def _submit(
    *,
    payload: ApplicationRequest,
    employee_id: uuid.UUID | None,
    on_behalf: bool,
    request: Request,
    response: Response,
    context: Any,
    db: Any,
    service: LeaveApplicationService,
    idempotency_key: uuid.UUID | None,
    idempotency: IdempotencyRepository,
) -> ApplicationResponse:
    guard = None
    if idempotency_key is not None:
        guard = idempotency_guard(
            repository=idempotency,
            context=context,
            key=idempotency_key,
            method="POST",
            path=str(request.url.path),
            payload=payload.model_dump(mode="json"),
        )
        replayed = guard.replay()
        if replayed is not None:
            return ApplicationResponse.model_validate(replayed)
    body = payload.model_dump(exclude={"acknowledged_warnings"})
    with unit_of_work(db):
        if guard is not None:
            guard.claim()
        view = service.submit(
            context,
            employee_id=employee_id,
            body=body,
            on_behalf=on_behalf,
            acknowledged=payload.acknowledged_warnings,
        )
        result = _respond(view, applications=service)
        if guard is not None:
            guard.complete(status_code=status.HTTP_201_CREATED, body=result.model_dump(mode="json"))
    response.status_code = status.HTTP_201_CREATED
    return _stamp(response, view, result)


@router.post(
    "/me/leave-applications",
    response_model=ApplicationResponse,
    status_code=status.HTTP_201_CREATED,
)
def submit_own_leave_application(
    payload: ApplicationRequest,
    request: Request,
    response: Response,
    context: CurrentOrg,
    db: DbSession,
    idempotency_key: IdempotencyKeyHeader,
    service: Applications,
    idempotency: IdempotencyStore,
) -> ApplicationResponse:
    return _submit(
        payload=payload,
        employee_id=None,
        on_behalf=False,
        request=request,
        response=response,
        context=context,
        db=db,
        service=service,
        idempotency_key=idempotency_key,
        idempotency=idempotency,
    )


@router.post(
    "/employees/{employee_id}/leave-applications",
    response_model=ApplicationResponse,
    status_code=status.HTTP_201_CREATED,
)
def submit_leave_application_on_behalf(
    employee_id: uuid.UUID,
    payload: OnBehalfApplicationRequest,
    request: Request,
    response: Response,
    context: CurrentOrg,
    db: DbSession,
    idempotency_key: IdempotencyKeyHeader,
    service: Applications,
    idempotency: IdempotencyStore,
) -> ApplicationResponse:
    return _submit(
        payload=payload,
        employee_id=employee_id,
        on_behalf=True,
        request=request,
        response=response,
        context=context,
        db=db,
        service=service,
        idempotency_key=idempotency_key,
        idempotency=idempotency,
    )


@router.get("/leave-applications", response_model=Page[ApplicationResponse])
def list_leave_applications(
    context: CurrentOrg,
    service: Applications,
    limit: PageLimit = 25,
    cursor: str | None = None,
    status_filter: Annotated[list[str] | None, Query(alias="status")] = None,
    employee_id: uuid.UUID | None = None,
    leave_type_id: uuid.UUID | None = None,
    from_date: Annotated[date | None, Query(alias="from")] = None,
    to_date: Annotated[date | None, Query(alias="to")] = None,
    approver: Annotated[str | None, Query(pattern="^me$")] = None,
    hr_queue: bool = False,
) -> Page[ApplicationResponse]:
    rows, next_cursor = service.list_applications(
        context,
        employee_id=employee_id,
        statuses=status_filter,
        leave_type_id=leave_type_id,
        from_date=from_date,
        to_date=to_date,
        approver_me=approver == "me",
        hr_queue=hr_queue,
        limit=limit,
        cursor=cursor,
    )
    return Page[ApplicationResponse](
        items=[_list_item(r, show_reason=_may_see_reason(context, r)) for r in rows],
        next_cursor=next_cursor,
    )


def _may_see_reason(context: OrgContext, row: LeaveApplication) -> bool:
    """List rows carry no action history, so only the owner and HR approvers see the reason."""
    return context.is_self(row.employee_id) or context.has(Permission.LEAVE_APPROVE_HR)


def _list_item(row: LeaveApplication, *, show_reason: bool) -> ApplicationResponse:
    result = ApplicationResponse.model_validate(row)
    result.reason = row.reason if show_reason else None
    result.supporting_document_attached = row.supporting_document_id is not None
    return result


@router.get("/me/leave-applications", response_model=Page[ApplicationResponse])
def list_own_leave_applications(
    context: CurrentOrg,
    service: Applications,
    limit: PageLimit = 25,
    cursor: str | None = None,
    status_filter: Annotated[list[str] | None, Query(alias="status")] = None,
) -> Page[ApplicationResponse]:
    rows, next_cursor = service.list_own(
        context, statuses=status_filter, limit=limit, cursor=cursor
    )
    return Page[ApplicationResponse](
        items=[_list_item(r, show_reason=True) for r in rows], next_cursor=next_cursor
    )


@router.get("/leave-applications/{application_id}", response_model=ApplicationResponse)
@router.get("/me/leave-applications/{application_id}", response_model=ApplicationResponse)
def read_leave_application(
    application_id: uuid.UUID, response: Response, context: CurrentOrg, service: Applications
) -> ApplicationResponse:
    view, show_reason = service.get(context, application_id)
    result = _respond(view, applications=service, show_reason=show_reason)
    return _stamp(response, view, result)


@router.post("/leave-applications/{application_id}/approve", response_model=ApplicationResponse)
def approve_leave_application(
    application_id: uuid.UUID,
    payload: ApproveRequest,
    response: Response,
    context: CurrentOrg,
    if_match: IfMatch,
    db: DbSession,
    service: Applications,
) -> ApplicationResponse:
    with unit_of_work(db):
        view = service.approve(
            context,
            application_id,
            comment=payload.comment,
            if_match=if_match,
            acknowledged=payload.acknowledged_warnings,
        )
        result = _respond(view, applications=service)
    return _stamp(response, view, result)


@router.post("/leave-applications/{application_id}/reject", response_model=ApplicationResponse)
def reject_leave_application(
    application_id: uuid.UUID,
    payload: RejectRequest,
    response: Response,
    context: CurrentOrg,
    if_match: IfMatch,
    db: DbSession,
    service: Applications,
) -> ApplicationResponse:
    with unit_of_work(db):
        view = service.reject(
            context, application_id, decision_comment=payload.decision_comment, if_match=if_match
        )
        result = _respond(view, applications=service)
    return _stamp(response, view, result)


@router.post("/leave-applications/{application_id}/withdraw", response_model=ApplicationResponse)
@router.post("/me/leave-applications/{application_id}/withdraw", response_model=ApplicationResponse)
def withdraw_leave_application(
    application_id: uuid.UUID,
    payload: WithdrawRequest,
    response: Response,
    context: CurrentOrg,
    if_match: IfMatch,
    db: DbSession,
    service: Applications,
) -> ApplicationResponse:
    with unit_of_work(db):
        view = service.withdraw(context, application_id, reason=payload.reason, if_match=if_match)
        result = _respond(view, applications=service)
    return _stamp(response, view, result)


@router.post("/leave-applications/{application_id}/cancel", response_model=ApplicationResponse)
@router.post("/me/leave-applications/{application_id}/cancel", response_model=ApplicationResponse)
def cancel_leave_application(
    application_id: uuid.UUID,
    payload: CancelRequest,
    response: Response,
    context: CurrentOrg,
    if_match: IfMatch,
    db: DbSession,
    service: Applications,
) -> ApplicationResponse:
    with unit_of_work(db):
        view, restored = service.cancel(
            context,
            application_id,
            scope=payload.scope,
            cancel_from_date=payload.cancel_from_date,
            cancel_from_half=payload.cancel_from_half,
            reason=payload.reason,
            restoration_mode=payload.restoration_mode,
            if_match=if_match,
        )
        result = _respond(view, applications=service, restored=restored)
    return _stamp(response, view, result)


@router.post("/leave-applications/{application_id}/reassign", response_model=ApplicationResponse)
def reassign_leave_application(
    application_id: uuid.UUID,
    payload: ReassignRequest,
    response: Response,
    context: CurrentOrg,
    if_match: IfMatch,
    db: DbSession,
    service: Applications,
) -> ApplicationResponse:
    with unit_of_work(db):
        view = service.reassign(
            context,
            application_id,
            to_user_id=payload.to_user_id,
            to_hr_queue=payload.to_hr_queue,
            reason=payload.reason,
            if_match=if_match,
        )
        result = _respond(view, applications=service)
    return _stamp(response, view, result)


@router.post("/leave-applications/{application_id}/recalculate", response_model=ApplicationResponse)
def recalculate_leave_application(
    application_id: uuid.UUID,
    payload: RecalculateRequest,
    response: Response,
    context: CurrentOrg,
    if_match: IfMatch,
    db: DbSession,
    service: Applications,
) -> ApplicationResponse:
    with unit_of_work(db):
        view = service.recalculate(
            context, application_id, reason=payload.reason, if_match=if_match
        )
        result = _respond(view, applications=service)
    return _stamp(response, view, result)


# ----------------------------------------------------------------------------- processing runs


@router.get("/leave-processing/runs", response_model=Page[JobRunResponse])
def list_processing_runs(
    context: CurrentOrg, db: DbSession, job_code: str | None = None
) -> Page[JobRunResponse]:
    context.require(Permission.LEAVE_PROCESS_RUN)
    rows = BackgroundJobRunRepository(db, context.organization_id).list_runs(job_code)
    return Page[JobRunResponse](items=[JobRunResponse.model_validate(r) for r in rows])


@router.post(
    "/leave-processing/runs", response_model=JobRunResponse, status_code=status.HTTP_201_CREATED
)
def trigger_processing_run(
    payload: JobRunRequest,
    context: CurrentOrg,
    db: DbSession,
    idempotency_key: IdempotencyKeyHeader,
) -> JobRunResponse:
    """§14: a manual run. The same Idempotency-Key never starts a second run."""
    context.require(Permission.LEAVE_PROCESS_RUN)
    key = require_idempotency_key(idempotency_key, endpoint="Triggering a leave processing run")
    jobs = LeaveJobs(db, context.organization_id)
    if not jobs.settings.enable_leave_module:
        raise ModuleNotEnabled()
    if payload.as_of_date > jobs.today():
        raise ValidationFailed("as_of_date cannot be in the future.")
    run_key = f"MANUAL:{key}"
    existing = BackgroundJobRunRepository(db, context.organization_id).by_run_key(
        payload.job_code, run_key
    )
    if existing is not None:
        return JobRunResponse.model_validate(existing)
    runner = jobs.run_accrual if payload.job_code == "LEAVE_ACCRUAL" else jobs.run_year_end
    run = runner(
        payload.as_of_date, run_key=run_key, trigger="MANUAL", requested_by=context.user_id
    )
    return JobRunResponse.model_validate(run)
