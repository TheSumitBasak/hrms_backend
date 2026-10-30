"""Onboarding: templates, instances, tasks, submissions and self-service (addendum 4.14, 11).

Handlers parse, delegate and serialise; every rule lives in the onboarding services.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, Response, status
from pydantic import BaseModel

from app.api.deps import (
    CurrentOrg,
    DbSession,
    IdempotencyKeyHeader,
    IfMatch,
    get_idempotency_repository,
    get_onboarding_service,
    get_submission_service,
    get_template_service,
)
from app.api.idempotency import idempotency_guard, require_idempotency_key
from app.core.context import OrgContext
from app.core.permissions import Permission
from app.db.session import unit_of_work
from app.models.onboarding import OnboardingDataSubmission, OnboardingTask, OnboardingTemplate
from app.repositories.platform import IdempotencyRepository
from app.schemas.common import Page, etag_for
from app.schemas.onboarding import (
    AcknowledgementOut,
    AdHocTaskRequest,
    CandidateOut,
    DueDateBody,
    InitiateRequest,
    InstanceEventOut,
    InstanceOut,
    InstanceUpdateRequest,
    LinkBody,
    PaperAckBody,
    ReasonBody,
    ReassignBody,
    RejectBody,
    ReopenInstanceBody,
    ReplaceTasksRequest,
    ResolveRequest,
    ResolveResponse,
    SelfSubmissionBody,
    StatusReason,
    SubmissionAcceptBody,
    SubmissionBody,
    SubmissionOut,
    SubmissionRejectBody,
    SubmissionWithdrawBody,
    TaskActionBody,
    TaskDetail,
    TaskDocumentOut,
    TaskEventOut,
    TaskOut,
    TaskReopenBody,
    TemplateCreateRequest,
    TemplateOut,
    TemplateTaskOut,
    TemplateUpdateRequest,
    UnlinkBody,
    VersionFields,
    VersionOut,
    WaiveBody,
)
from app.services.onboarding import InstanceView, OnboardingService, TaskState
from app.services.onboarding_submissions import SubmissionService
from app.services.onboarding_templates import TemplateService

router = APIRouter(prefix="/orgs/{org_id}", tags=["Onboarding"])

Templates = Annotated[TemplateService, Depends(get_template_service)]
Onboarding = Annotated[OnboardingService, Depends(get_onboarding_service)]
Submissions = Annotated[SubmissionService, Depends(get_submission_service)]
IdempotencyStore = Annotated[IdempotencyRepository, Depends(get_idempotency_repository)]


def _etag(response: Response, row: Any) -> None:
    response.headers["ETag"] = etag_for(row.row_version)


def _idempotent[ModelT: BaseModel](
    *,
    request: Request,
    context: OrgContext,
    db: Any,
    key: uuid.UUID | None,
    store: IdempotencyRepository,
    payload: dict[str, Any],
    model: type[ModelT],
    run: Callable[[], ModelT],
    required: str | None = None,
) -> ModelT:
    """Run ``run`` once per Idempotency-Key (24 h replay); the key is claimed in the same txn."""
    guard = None
    if required is not None:
        key = require_idempotency_key(key, endpoint=required)
    if key is not None:
        guard = idempotency_guard(
            repository=store,
            context=context,
            key=key,
            method="POST",
            path=str(request.url.path),
            payload=payload,
        )
        replayed = guard.replay()
        if replayed is not None:
            return model.model_validate(replayed)
    with unit_of_work(db):
        if guard is not None:
            guard.claim()
        result = run()
        if guard is not None:
            guard.complete(status_code=status.HTTP_201_CREATED, body=result.model_dump(mode="json"))
    return result


# Serialisers (with the 10.5 / 11 visibility rules).


def _employee_only(context: OrgContext) -> bool:
    """SELF users without onboarding.view see only employee-visible tasks and no internals."""
    return not context.has(Permission.ONBOARDING_VIEW)


def _task_out(state: TaskState, context: OrgContext) -> TaskOut:
    out = TaskOut.model_validate(state.task)
    out.is_blocked, out.is_overdue = state.is_blocked, state.is_overdue
    if _employee_only(context):
        out.assignee_user_id = out.assignee_department_id = out.reviewer_user_id = None
        out.assignee_resolution = None
    return out


def _instance_out(view: InstanceView, context: OrgContext) -> InstanceOut:
    out = InstanceOut.model_validate(view.instance)
    for name in (
        "phase",
        "mandatory_total",
        "mandatory_closed",
        "optional_total",
        "optional_closed",
        "overdue_count",
        "progress_percent",
    ):
        setattr(out, name, getattr(view, name))
    employee_only = _employee_only(context)
    states = [s for s in view.tasks if s.task.visible_to_employee] if employee_only else view.tasks
    out.tasks = [_task_out(s, context) for s in states]
    out.events = [] if employee_only else [InstanceEventOut.model_validate(e) for e in view.events]
    if employee_only:
        out.remarks = out.hr_coordinator_user_id = None  # internal remarks are never shown to SELF
    out.warnings = view.warnings
    return out


def _stamp(response: Response, out: Any, row: Any) -> Any:
    _etag(response, row)
    out.row_version = row.row_version
    return out


def _template_out(
    template: OnboardingTemplate, versions: list[Any], service: TemplateService
) -> TemplateOut:
    out = TemplateOut.model_validate(template)
    out.versions = [VersionOut.model_validate(v) for v in versions]
    return out


# Templates.


@router.get("/onboarding-templates", response_model=Page[TemplateOut])
def list_templates(
    context: CurrentOrg,
    service: Templates,
    status_filter: Annotated[
        str | None, Query(alias="status", pattern="^(ACTIVE|INACTIVE)$")
    ] = None,
) -> Page[TemplateOut]:
    rows, versions = service.list_templates(context, status=status_filter)
    return Page[TemplateOut](items=[_template_out(r, versions[r.id], service) for r in rows])


@router.post(
    "/onboarding-templates", response_model=TemplateOut, status_code=status.HTTP_201_CREATED
)
def create_template(
    payload: TemplateCreateRequest,
    request: Request,
    response: Response,
    context: CurrentOrg,
    db: DbSession,
    idempotency_key: IdempotencyKeyHeader,
    service: Templates,
    store: IdempotencyStore,
) -> TemplateOut:
    def run() -> TemplateOut:
        template = service.create_template(
            context,
            values={
                "template_code": payload.template_code,
                "template_name": payload.template_name,
                "description": payload.description,
            },
            version=payload.version.model_dump(exclude_none=True),
            tasks=[t.model_dump() for t in payload.tasks],
            dependencies=[d.model_dump() for d in payload.dependencies],
        )
        _, versions = service.get_template(context, template.id)
        return _template_out(template, versions, service)

    result = _idempotent(
        request=request,
        context=context,
        db=db,
        key=idempotency_key,
        store=store,
        payload=payload.model_dump(mode="json"),
        model=TemplateOut,
        run=run,
    )
    response.status_code = status.HTTP_201_CREATED
    return result


@router.post("/onboarding-templates/resolve", response_model=ResolveResponse)
def resolve_template(
    payload: ResolveRequest, context: CurrentOrg, service: Templates, onboarding: Onboarding
) -> ResolveResponse:
    context.require(Permission.ONBOARDING_INITIATE)
    employee = onboarding.visible_employee(context, payload.employee_id)
    selection = service.select(context, employee, basis_date=payload.basis_date)

    def candidate(pair: tuple[Any, Any]) -> CandidateOut:
        v, t = pair
        return CandidateOut(
            template_id=t.id,
            template_code=t.template_code,
            version_id=v.id,
            version_number=v.version_number,
            applicability_specificity=v.applicability_specificity,
            priority=v.priority,
            effective_from=v.effective_from,
        )

    return ResolveResponse(
        selected=candidate((selection.version, selection.template)),
        candidates=[candidate(p) for p in selection.candidates],
    )


@router.get("/onboarding-templates/{template_id}", response_model=TemplateOut)
def read_template(
    template_id: uuid.UUID, response: Response, context: CurrentOrg, service: Templates
) -> TemplateOut:
    template, versions = service.get_template(context, template_id)
    _etag(response, template)
    return _template_out(template, versions, service)


@router.patch("/onboarding-templates/{template_id}", response_model=TemplateOut)
def update_template(
    template_id: uuid.UUID,
    payload: TemplateUpdateRequest,
    response: Response,
    context: CurrentOrg,
    if_match: IfMatch,
    db: DbSession,
    service: Templates,
) -> TemplateOut:
    with unit_of_work(db):
        template = service.update_template(
            context,
            template_id=template_id,
            changes=payload.model_dump(exclude_unset=True, exclude_none=True),
            if_match=if_match,
        )
        _, versions = service.get_template(context, template_id)
        out = _template_out(template, versions, service)
    return _stamp(response, out, template)  # type: ignore[no-any-return]


@router.post("/onboarding-templates/{template_id}/deactivate", response_model=TemplateOut)
@router.post("/onboarding-templates/{template_id}/reactivate", response_model=TemplateOut)
def change_template_status(
    template_id: uuid.UUID,
    payload: StatusReason,
    request: Request,
    response: Response,
    context: CurrentOrg,
    db: DbSession,
    service: Templates,
) -> TemplateOut:
    target = "INACTIVE" if request.url.path.endswith("/deactivate") else "ACTIVE"
    with unit_of_work(db):
        template = service.set_status(
            context, template_id=template_id, status=target, reason=payload.reason
        )
        _, versions = service.get_template(context, template_id)
        out = _template_out(template, versions, service)
    return _stamp(response, out, template)  # type: ignore[no-any-return]


def _version_out(service: TemplateService, version: Any, tasks: list[Any]) -> VersionOut:
    out = VersionOut.model_validate(version)
    codes = {t.id: t.task_code for t in tasks}
    out.tasks = [TemplateTaskOut.model_validate(t) for t in tasks]
    out.dependencies = [
        {"task_code": codes[d.task_id], "prerequisite_task_code": codes[d.prerequisite_task_id]}  # type: ignore[misc]
        for d in service.dependencies_of(version.id)
    ]
    return out


@router.post(
    "/onboarding-templates/{template_id}/versions",
    response_model=VersionOut,
    status_code=status.HTTP_201_CREATED,
)
def new_version(
    template_id: uuid.UUID,
    response: Response,
    context: CurrentOrg,
    db: DbSession,
    service: Templates,
) -> VersionOut:
    with unit_of_work(db):
        version = service.new_version(context, template_id)
        _, tasks, _ = service.get_version(context, template_id, version.id)
        out = _version_out(service, version, tasks)
    return _stamp(response, out, version)  # type: ignore[no-any-return]


@router.get("/onboarding-templates/{template_id}/versions/{version_id}", response_model=VersionOut)
def read_version(
    template_id: uuid.UUID,
    version_id: uuid.UUID,
    response: Response,
    context: CurrentOrg,
    service: Templates,
) -> VersionOut:
    version, tasks, _ = service.get_version(context, template_id, version_id)
    _etag(response, version)
    return _version_out(service, version, tasks)


@router.patch(
    "/onboarding-templates/{template_id}/versions/{version_id}", response_model=VersionOut
)
def update_version(
    template_id: uuid.UUID,
    version_id: uuid.UUID,
    payload: VersionFields,
    response: Response,
    context: CurrentOrg,
    if_match: IfMatch,
    db: DbSession,
    service: Templates,
) -> VersionOut:
    with unit_of_work(db):
        version = service.update_version(
            context,
            template_id=template_id,
            version_id=version_id,
            changes=payload.model_dump(exclude_unset=True),
            if_match=if_match,
        )
        _, tasks, _ = service.get_version(context, template_id, version_id)
        out = _version_out(service, version, tasks)
    return _stamp(response, out, version)  # type: ignore[no-any-return]


@router.put(
    "/onboarding-templates/{template_id}/versions/{version_id}/tasks", response_model=VersionOut
)
def replace_tasks(
    template_id: uuid.UUID,
    version_id: uuid.UUID,
    payload: ReplaceTasksRequest,
    response: Response,
    context: CurrentOrg,
    db: DbSession,
    service: Templates,
) -> VersionOut:
    with unit_of_work(db):
        service.replace_tasks(
            context,
            template_id=template_id,
            version_id=version_id,
            tasks=[t.model_dump() for t in payload.tasks],
            dependencies=[d.model_dump() for d in payload.dependencies],
        )
        version, tasks, _ = service.get_version(context, template_id, version_id)
        out = _version_out(service, version, tasks)
    return _stamp(response, out, version)  # type: ignore[no-any-return]


@router.post(
    "/onboarding-templates/{template_id}/versions/{version_id}/publish", response_model=VersionOut
)
def publish_version(
    template_id: uuid.UUID,
    version_id: uuid.UUID,
    response: Response,
    context: CurrentOrg,
    if_match: IfMatch,
    db: DbSession,
    service: Templates,
) -> VersionOut:
    with unit_of_work(db):
        version = service.publish(
            context, template_id=template_id, version_id=version_id, if_match=if_match
        )
        _, tasks, _ = service.get_version(context, template_id, version_id)
        out = _version_out(service, version, tasks)
    return _stamp(response, out, version)  # type: ignore[no-any-return]


@router.post(
    "/onboarding-templates/{template_id}/versions/{version_id}/archive", response_model=VersionOut
)
def archive_version(
    template_id: uuid.UUID,
    version_id: uuid.UUID,
    payload: StatusReason,
    response: Response,
    context: CurrentOrg,
    if_match: IfMatch,
    db: DbSession,
    service: Templates,
) -> VersionOut:
    with unit_of_work(db):
        version = service.archive(
            context,
            template_id=template_id,
            version_id=version_id,
            reason=payload.reason,
            if_match=if_match,
        )
        _, tasks, _ = service.get_version(context, template_id, version_id)
        out = _version_out(service, version, tasks)
    return _stamp(response, out, version)  # type: ignore[no-any-return]


# Instances.


@router.post(
    "/employees/{employee_id}/onboarding",
    response_model=InstanceOut,
    status_code=status.HTTP_201_CREATED,
)
def initiate(
    employee_id: uuid.UUID,
    payload: InitiateRequest,
    request: Request,
    response: Response,
    context: CurrentOrg,
    db: DbSession,
    idempotency_key: IdempotencyKeyHeader,
    service: Onboarding,
    store: IdempotencyStore,
) -> InstanceOut:
    def run() -> InstanceOut:
        view = service.initiate(
            context,
            employee_id=employee_id,
            template_id=payload.template_id,
            hr_coordinator_user_id=payload.hr_coordinator_user_id,
            target_completion_date=payload.target_completion_date,
            remarks=payload.remarks,
            acknowledged=payload.acknowledged_warnings,
        )
        return _instance_out(view, context)

    result = _idempotent(
        request=request,
        context=context,
        db=db,
        key=idempotency_key,
        store=store,
        payload=payload.model_dump(mode="json") | {"employee_id": str(employee_id)},
        model=InstanceOut,
        run=run,
        required="Initiating onboarding",
    )
    response.status_code = status.HTTP_201_CREATED
    return result


@router.get("/employees/{employee_id}/onboarding", response_model=Page[InstanceOut])
def employee_instances(
    employee_id: uuid.UUID,
    context: CurrentOrg,
    service: Onboarding,
    include_cancelled: bool = False,
) -> Page[InstanceOut]:
    views = service.for_employee(context, employee_id, include_cancelled=include_cancelled)
    return Page[InstanceOut](items=[_instance_out(v, context) for v in views])


@router.get("/onboarding", response_model=Page[InstanceOut])
def list_instances(
    context: CurrentOrg,
    service: Onboarding,
    status_filter: Annotated[list[str] | None, Query(alias="status")] = None,
) -> Page[InstanceOut]:
    views = service.list_instances(context, statuses=status_filter)
    return Page[InstanceOut](items=[_instance_out(v, context) for v in views])


@router.get("/onboarding-tasks", response_model=Page[TaskOut])
def task_queue(
    context: CurrentOrg,
    service: Onboarding,
    review_queue: bool = False,
    status_filter: Annotated[list[str] | None, Query(alias="status")] = None,
) -> Page[TaskOut]:
    rows = service.task_queue(context, review=review_queue, statuses=status_filter)
    return Page[TaskOut](items=[TaskOut.model_validate(r) for r in rows])


@router.get("/onboarding/{instance_id}", response_model=InstanceOut)
def read_instance(
    instance_id: uuid.UUID, response: Response, context: CurrentOrg, service: Onboarding
) -> InstanceOut:
    view = service.get(context, instance_id)
    return _stamp(response, _instance_out(view, context), view.instance)  # type: ignore[no-any-return]


def _finish_instance_call(
    response: Response, context: OrgContext, view: InstanceView
) -> InstanceOut:
    return _stamp(response, _instance_out(view, context), view.instance)  # type: ignore[no-any-return]


@router.patch("/onboarding/{instance_id}", response_model=InstanceOut)
def update_instance(
    instance_id: uuid.UUID,
    payload: InstanceUpdateRequest,
    response: Response,
    context: CurrentOrg,
    if_match: IfMatch,
    db: DbSession,
    service: Onboarding,
) -> InstanceOut:
    with unit_of_work(db):
        view = service.update_instance(
            context, instance_id, changes=payload.model_dump(exclude_unset=True), if_match=if_match
        )
        out = _instance_out(view, context)
    return _stamp(response, out, view.instance)  # type: ignore[no-any-return]


@router.post("/onboarding/{instance_id}/complete", response_model=InstanceOut)
def complete_instance(
    instance_id: uuid.UUID,
    response: Response,
    context: CurrentOrg,
    if_match: IfMatch,
    db: DbSession,
    service: Onboarding,
) -> InstanceOut:
    with unit_of_work(db):
        view = service.complete(context, instance_id, if_match=if_match)
        out = _instance_out(view, context)
    return _stamp(response, out, view.instance)  # type: ignore[no-any-return]


@router.post("/onboarding/{instance_id}/cancel", response_model=InstanceOut)
def cancel_instance(
    instance_id: uuid.UUID,
    payload: ReasonBody,
    response: Response,
    context: CurrentOrg,
    if_match: IfMatch,
    db: DbSession,
    service: Onboarding,
) -> InstanceOut:
    with unit_of_work(db):
        view = service.cancel(context, instance_id, reason=payload.reason, if_match=if_match)
        out = _instance_out(view, context)
    return _stamp(response, out, view.instance)  # type: ignore[no-any-return]


@router.post("/onboarding/{instance_id}/reopen", response_model=InstanceOut)
def reopen_instance(
    instance_id: uuid.UUID,
    payload: ReopenInstanceBody,
    response: Response,
    context: CurrentOrg,
    if_match: IfMatch,
    db: DbSession,
    service: Onboarding,
) -> InstanceOut:
    with unit_of_work(db):
        view = service.reopen(context, instance_id, reason=payload.reason, if_match=if_match)
        out = _instance_out(view, context)
    return _stamp(response, out, view.instance)  # type: ignore[no-any-return]


# Tasks.


def _task_response(response: Response, task: OnboardingTask, context: OrgContext) -> TaskOut:
    out = TaskOut.model_validate(task)
    if _employee_only(context):
        out.assignee_user_id = out.assignee_department_id = out.reviewer_user_id = None
    return _stamp(response, out, task)  # type: ignore[no-any-return]


@router.post(
    "/onboarding/{instance_id}/tasks", response_model=TaskOut, status_code=status.HTTP_201_CREATED
)
def add_task(
    instance_id: uuid.UUID,
    payload: AdHocTaskRequest,
    request: Request,
    response: Response,
    context: CurrentOrg,
    db: DbSession,
    idempotency_key: IdempotencyKeyHeader,
    service: Onboarding,
    store: IdempotencyStore,
) -> TaskOut:
    def run() -> TaskOut:
        task = service.add_task(
            context,
            instance_id,
            values=payload.model_dump(exclude={"prerequisite_task_ids"}),
            prerequisite_ids=payload.prerequisite_task_ids,
        )
        return TaskOut.model_validate(task)

    result = _idempotent(
        request=request,
        context=context,
        db=db,
        key=idempotency_key,
        store=store,
        payload=payload.model_dump(mode="json"),
        model=TaskOut,
        run=run,
        required="Adding an onboarding task",
    )
    response.status_code = status.HTTP_201_CREATED
    return result


@router.get("/onboarding/{instance_id}/tasks/{task_id}", response_model=TaskDetail)
def read_task(
    instance_id: uuid.UUID,
    task_id: uuid.UUID,
    response: Response,
    context: CurrentOrg,
    service: Onboarding,
) -> TaskDetail:
    task, events, documents = service.get_task(context, instance_id, task_id)
    out = TaskDetail.model_validate(task)
    out.events = [TaskEventOut.model_validate(e) for e in events]
    out.documents = [TaskDocumentOut.model_validate(d) for d in documents]
    if _employee_only(context):
        out.assignee_user_id = out.assignee_department_id = out.reviewer_user_id = None
        out.events = []
    return _stamp(response, out, task)  # type: ignore[no-any-return]


@router.post("/onboarding/{instance_id}/tasks/{task_id}/start", response_model=TaskOut)
def start_task(
    instance_id: uuid.UUID,
    task_id: uuid.UUID,
    payload: TaskActionBody,
    response: Response,
    context: CurrentOrg,
    if_match: IfMatch,
    db: DbSession,
    service: Onboarding,
) -> TaskOut:
    with unit_of_work(db):
        task = service.start(
            context,
            instance_id,
            task_id,
            if_match=if_match,
            on_behalf_reason=payload.on_behalf_reason,
        )
        out = TaskOut.model_validate(task)
    return _stamp(response, out, task)  # type: ignore[no-any-return]


@router.post("/onboarding/{instance_id}/tasks/{task_id}/complete", response_model=TaskOut)
def complete_task(
    instance_id: uuid.UUID,
    task_id: uuid.UUID,
    payload: TaskActionBody,
    response: Response,
    context: CurrentOrg,
    if_match: IfMatch,
    db: DbSession,
    service: Onboarding,
) -> TaskOut:
    with unit_of_work(db):
        task = service.complete_task(
            context,
            instance_id,
            task_id,
            if_match=if_match,
            completion_remarks=payload.completion_remarks,
            on_behalf_reason=payload.on_behalf_reason,
        )
        out = TaskOut.model_validate(task)
    return _stamp(response, out, task)  # type: ignore[no-any-return]


@router.post("/onboarding/{instance_id}/tasks/{task_id}/submit", response_model=TaskOut)
@router.post("/onboarding/{instance_id}/tasks/{task_id}/resubmit", response_model=TaskOut)
def submit_task(
    instance_id: uuid.UUID,
    task_id: uuid.UUID,
    payload: TaskActionBody,
    request: Request,
    response: Response,
    context: CurrentOrg,
    if_match: IfMatch,
    db: DbSession,
    service: Onboarding,
) -> TaskOut:
    with unit_of_work(db):
        task = service.submit_task(
            context,
            instance_id,
            task_id,
            if_match=if_match,
            resubmit=request.url.path.endswith("/resubmit"),
            on_behalf_reason=payload.on_behalf_reason,
        )
        out = TaskOut.model_validate(task)
    return _stamp(response, out, task)  # type: ignore[no-any-return]


@router.post("/onboarding/{instance_id}/tasks/{task_id}/accept", response_model=TaskOut)
def accept_task(
    instance_id: uuid.UUID,
    task_id: uuid.UUID,
    response: Response,
    context: CurrentOrg,
    if_match: IfMatch,
    db: DbSession,
    service: Onboarding,
) -> TaskOut:
    with unit_of_work(db):
        task = service.accept_task(context, instance_id, task_id, if_match=if_match)
        out = TaskOut.model_validate(task)
    return _stamp(response, out, task)  # type: ignore[no-any-return]


@router.post("/onboarding/{instance_id}/tasks/{task_id}/reject", response_model=TaskOut)
def reject_task(
    instance_id: uuid.UUID,
    task_id: uuid.UUID,
    payload: RejectBody,
    response: Response,
    context: CurrentOrg,
    if_match: IfMatch,
    db: DbSession,
    service: Onboarding,
) -> TaskOut:
    with unit_of_work(db):
        task = service.reject_task(
            context, instance_id, task_id, reason=payload.rejection_reason, if_match=if_match
        )
        out = TaskOut.model_validate(task)
    return _stamp(response, out, task)  # type: ignore[no-any-return]


@router.post("/onboarding/{instance_id}/tasks/{task_id}/waive", response_model=TaskOut)
def waive_task(
    instance_id: uuid.UUID,
    task_id: uuid.UUID,
    payload: WaiveBody,
    response: Response,
    context: CurrentOrg,
    if_match: IfMatch,
    db: DbSession,
    service: Onboarding,
) -> TaskOut:
    with unit_of_work(db):
        task = service.waive(
            context, instance_id, task_id, reason=payload.waiver_reason, if_match=if_match
        )
        out = TaskOut.model_validate(task)
    return _stamp(response, out, task)  # type: ignore[no-any-return]


@router.post("/onboarding/{instance_id}/tasks/{task_id}/reopen", response_model=TaskOut)
def reopen_task(
    instance_id: uuid.UUID,
    task_id: uuid.UUID,
    payload: TaskReopenBody,
    response: Response,
    context: CurrentOrg,
    if_match: IfMatch,
    db: DbSession,
    service: Onboarding,
) -> TaskOut:
    with unit_of_work(db):
        task = service.reopen_task(
            context,
            instance_id,
            task_id,
            reason=payload.reason,
            if_match=if_match,
            acknowledged=payload.acknowledged_warnings,
        )
        out = TaskOut.model_validate(task)
    return _stamp(response, out, task)  # type: ignore[no-any-return]


@router.post("/onboarding/{instance_id}/tasks/{task_id}/reassign", response_model=TaskOut)
def reassign_task(
    instance_id: uuid.UUID,
    task_id: uuid.UUID,
    payload: ReassignBody,
    response: Response,
    context: CurrentOrg,
    if_match: IfMatch,
    db: DbSession,
    service: Onboarding,
) -> TaskOut:
    with unit_of_work(db):
        task = service.reassign(
            context,
            instance_id,
            task_id,
            assignee_user_id=payload.assignee_user_id,
            assignee_department_id=payload.assignee_department_id,
            reason=payload.reason,
            if_match=if_match,
        )
        out = TaskOut.model_validate(task)
    return _stamp(response, out, task)  # type: ignore[no-any-return]


@router.post("/onboarding/{instance_id}/tasks/{task_id}/due-date", response_model=TaskOut)
def set_due_date(
    instance_id: uuid.UUID,
    task_id: uuid.UUID,
    payload: DueDateBody,
    response: Response,
    context: CurrentOrg,
    if_match: IfMatch,
    db: DbSession,
    service: Onboarding,
) -> TaskOut:
    with unit_of_work(db):
        task = service.set_due_date(
            context,
            instance_id,
            task_id,
            new_date=payload.due_date,
            reason=payload.reason,
            if_match=if_match,
        )
        out = TaskOut.model_validate(task)
    return _stamp(response, out, task)  # type: ignore[no-any-return]


@router.post("/onboarding/{instance_id}/tasks/{task_id}/cancel", response_model=TaskOut)
def cancel_task(
    instance_id: uuid.UUID,
    task_id: uuid.UUID,
    payload: ReasonBody,
    response: Response,
    context: CurrentOrg,
    if_match: IfMatch,
    db: DbSession,
    service: Onboarding,
) -> TaskOut:
    with unit_of_work(db):
        task = service.remove_adhoc_task(
            context, instance_id, task_id, reason=payload.reason, if_match=if_match
        )
        out = TaskOut.model_validate(task)
    return _stamp(response, out, task)  # type: ignore[no-any-return]


# Documents and acknowledgements.


@router.post(
    "/onboarding/{instance_id}/tasks/{task_id}/documents",
    response_model=TaskDocumentOut,
    status_code=status.HTTP_201_CREATED,
)
def link_document(
    instance_id: uuid.UUID,
    task_id: uuid.UUID,
    payload: LinkBody,
    context: CurrentOrg,
    db: DbSession,
    service: Onboarding,
) -> TaskDocumentOut:
    with unit_of_work(db):
        link = service.link_document(context, instance_id, task_id, document_id=payload.document_id)
        return TaskDocumentOut.model_validate(link)


@router.post(
    "/onboarding/{instance_id}/tasks/{task_id}/documents/{link_id}/unlink",
    response_model=TaskDocumentOut,
)
def unlink_document(
    instance_id: uuid.UUID,
    task_id: uuid.UUID,
    link_id: uuid.UUID,
    payload: UnlinkBody,
    context: CurrentOrg,
    db: DbSession,
    service: Onboarding,
) -> TaskDocumentOut:
    with unit_of_work(db):
        link = service.unlink_document(
            context, instance_id, task_id, link_id, reason=payload.reason
        )
        return TaskDocumentOut.model_validate(link)


@router.post(
    "/onboarding/{instance_id}/tasks/{task_id}/acknowledgements",
    response_model=AcknowledgementOut,
    status_code=status.HTTP_201_CREATED,
)
def paper_acknowledgement(
    instance_id: uuid.UUID,
    task_id: uuid.UUID,
    payload: PaperAckBody,
    context: CurrentOrg,
    db: DbSession,
    service: Onboarding,
) -> AcknowledgementOut:
    with unit_of_work(db):
        row = service.acknowledge(
            context,
            instance_id,
            task_id,
            paper=True,
            supporting_document_id=payload.supporting_document_id,
            delivered_at=payload.delivered_at,
            on_behalf_reason=payload.on_behalf_reason,
        )
        return AcknowledgementOut.model_validate(row)


# Submissions.


def _submission(row: OnboardingDataSubmission) -> SubmissionOut:
    return SubmissionOut.model_validate(row)


@router.post(
    "/onboarding/{instance_id}/tasks/{task_id}/submissions",
    response_model=SubmissionOut,
    status_code=status.HTTP_201_CREATED,
)
def submit_on_behalf(
    instance_id: uuid.UUID,
    task_id: uuid.UUID,
    payload: SubmissionBody,
    request: Request,
    response: Response,
    context: CurrentOrg,
    db: DbSession,
    idempotency_key: IdempotencyKeyHeader,
    service: Submissions,
    store: IdempotencyStore,
) -> SubmissionOut:
    def run() -> SubmissionOut:
        row = service.submit(
            context,
            instance_id,
            task_id,
            payload=payload.payload,
            on_behalf=True,
            on_behalf_reason=payload.on_behalf_reason,
        )
        return _submission(row)

    result = _idempotent(
        request=request,
        context=context,
        db=db,
        key=idempotency_key,
        store=store,
        payload={"task": str(task_id), "fields": sorted(payload.payload)},
        model=SubmissionOut,
        run=run,
        required="Submitting data",
    )
    response.status_code = status.HTTP_201_CREATED
    return result


@router.get(
    "/onboarding/{instance_id}/tasks/{task_id}/submissions", response_model=Page[SubmissionOut]
)
def list_submissions(
    instance_id: uuid.UUID, task_id: uuid.UUID, context: CurrentOrg, service: Submissions
) -> Page[SubmissionOut]:
    rows = service.list_for_task(context, instance_id, task_id)
    return Page[SubmissionOut](items=[_submission(r) for r in rows])


@router.post(
    "/onboarding/{instance_id}/tasks/{task_id}/submissions/{submission_id}/accept",
    response_model=SubmissionOut,
)
def accept_submission(
    instance_id: uuid.UUID,
    task_id: uuid.UUID,
    submission_id: uuid.UUID,
    payload: SubmissionAcceptBody,
    response: Response,
    context: CurrentOrg,
    if_match: IfMatch,
    db: DbSession,
    service: Submissions,
) -> SubmissionOut:
    with unit_of_work(db):
        row = service.accept(
            context,
            instance_id,
            task_id,
            submission_id,
            acknowledged=payload.acknowledged_warnings,
            if_match=if_match,
        )
        out = _submission(row)
    return _stamp(response, out, row)  # type: ignore[no-any-return]


@router.post(
    "/onboarding/{instance_id}/tasks/{task_id}/submissions/{submission_id}/reject",
    response_model=SubmissionOut,
)
def reject_submission(
    instance_id: uuid.UUID,
    task_id: uuid.UUID,
    submission_id: uuid.UUID,
    payload: SubmissionRejectBody,
    response: Response,
    context: CurrentOrg,
    if_match: IfMatch,
    db: DbSession,
    service: Submissions,
) -> SubmissionOut:
    with unit_of_work(db):
        row = service.reject(
            context,
            instance_id,
            task_id,
            submission_id,
            reason=payload.rejection_reason,
            field_comments=[c.model_dump() for c in payload.field_comments]
            if payload.field_comments
            else None,
            if_match=if_match,
        )
        out = _submission(row)
    return _stamp(response, out, row)  # type: ignore[no-any-return]


@router.post(
    "/onboarding/{instance_id}/tasks/{task_id}/submissions/{submission_id}/withdraw",
    response_model=SubmissionOut,
)
def withdraw_submission(
    instance_id: uuid.UUID,
    task_id: uuid.UUID,
    submission_id: uuid.UUID,
    payload: SubmissionWithdrawBody,
    response: Response,
    context: CurrentOrg,
    if_match: IfMatch,
    db: DbSession,
    service: Submissions,
) -> SubmissionOut:
    with unit_of_work(db):
        row = service.withdraw(
            context, instance_id, task_id, submission_id, reason=payload.reason, if_match=if_match
        )
        out = _submission(row)
    return _stamp(response, out, row)  # type: ignore[no-any-return]


# Employee self-service (11): identity comes from the user link, never from a path.


@router.get("/me/onboarding", response_model=InstanceOut | None)
def my_onboarding(context: CurrentOrg, service: Onboarding) -> InstanceOut | None:
    view = service.own_instance(context)
    return _instance_out(view, context) if view is not None else None


def _own_task(service: Onboarding, context: OrgContext) -> uuid.UUID:
    return service.own_employee_id(context)


@router.post("/me/onboarding/tasks/{task_id}/start", response_model=TaskOut)
@router.post("/me/onboarding/tasks/{task_id}/complete", response_model=TaskOut)
@router.post("/me/onboarding/tasks/{task_id}/submit", response_model=TaskOut)
def my_task_action(
    task_id: uuid.UUID,
    request: Request,
    response: Response,
    context: CurrentOrg,
    if_match: IfMatch,
    db: DbSession,
    service: Onboarding,
) -> TaskOut:
    employee_id = _own_task(service, context)
    verb = request.url.path.rsplit("/", 1)[-1]
    with unit_of_work(db):
        instance_id = service.own_task_instance(employee_id, task_id)
        action = {"start": service.start, "complete": service.complete_task}.get(verb)
        task = (
            action(context, instance_id, task_id, if_match=if_match)
            if action
            else service.submit_task(context, instance_id, task_id, if_match=if_match)
        )
        out = TaskOut.model_validate(task)
    out.assignee_user_id = out.assignee_department_id = out.reviewer_user_id = None
    return _stamp(response, out, task)  # type: ignore[no-any-return]


@router.post(
    "/me/onboarding/tasks/{task_id}/acknowledge",
    response_model=AcknowledgementOut,
    status_code=status.HTTP_201_CREATED,
)
def my_acknowledge(
    task_id: uuid.UUID, context: CurrentOrg, db: DbSession, service: Onboarding
) -> AcknowledgementOut:
    employee_id = _own_task(service, context)
    with unit_of_work(db):
        instance_id = service.own_task_instance(employee_id, task_id)
        row = service.acknowledge(context, instance_id, task_id, paper=False)
        return AcknowledgementOut.model_validate(row)


@router.post(
    "/me/onboarding/tasks/{task_id}/submissions",
    response_model=SubmissionOut,
    status_code=status.HTTP_201_CREATED,
)
def my_submission(
    task_id: uuid.UUID,
    payload: SelfSubmissionBody,
    request: Request,
    response: Response,
    context: CurrentOrg,
    db: DbSession,
    idempotency_key: IdempotencyKeyHeader,
    onboarding: Onboarding,
    service: Submissions,
    store: IdempotencyStore,
) -> SubmissionOut:
    employee_id = _own_task(onboarding, context)

    def run() -> SubmissionOut:
        instance_id = onboarding.own_task_instance(employee_id, task_id)
        row = service.submit(
            context,
            instance_id,
            task_id,
            payload=payload.payload,
            on_behalf=False,
            on_behalf_reason=None,
        )
        return _submission(row)

    result = _idempotent(
        request=request,
        context=context,
        db=db,
        key=idempotency_key,
        store=store,
        payload={"task": str(task_id), "fields": sorted(payload.payload)},
        model=SubmissionOut,
        run=run,
        required="Submitting data",
    )
    response.status_code = status.HTTP_201_CREATED
    return result
