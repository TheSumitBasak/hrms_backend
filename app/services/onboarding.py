"""Onboarding instances, tasks, documents and acknowledgements (addendum 4.2-4.9, 4.11, 4.12).

Lock order (7.3): employee row, then the instance, then the instance's tasks ascending by id.
Every state-changing use case takes that order, so activation, task transitions and completion
serialise per employee. Nothing here commits.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy.exc import IntegrityError

from app.audit.actions import AuditAction
from app.audit.writer import AuditWriter
from app.core.clock import today_in, utc_now
from app.core.context import OrgContext
from app.core.errors import (
    DocumentTaskMismatch,
    ErrorDetail,
    InvalidOnboardingTaskTransition,
    InvalidOnboardingTransition,
    ModuleNotEnabled,
    OnboardingActivationBlocked,
    OnboardingAlreadyExists,
    OnboardingEmployeeNotEligible,
    OnboardingInstanceNotOpen,
    OnboardingMandatoryTasksIncomplete,
    OnboardingReopenWindowExpired,
    OnboardingRequiredForActivation,
    OnboardingTaskBlocked,
    OnboardingTaskRequirementsUnmet,
    OnboardingTemplateInvalid,
    OrgAccessDenied,
    PermissionDenied,
    ResourceNotFound,
    SelfReviewNotAllowed,
    SelfServiceNotEnabled,
    TaskNotWaivable,
    ValidationFailed,
)
from app.core.permissions import Permission
from app.core.warnings import WarningCode, WarningCollector
from app.db.types import uuid7
from app.models.employee import SEPARATED_STATUSES, Employee, EmployeeStatus
from app.models.onboarding import (
    OnboardingAcknowledgement,
    OnboardingInstance,
    OnboardingInstanceEvent,
    OnboardingTask,
    OnboardingTaskDependency,
    OnboardingTaskDocument,
    OnboardingTaskEvent,
    OnboardingTemplateTask,
)
from app.models.privacy import EmployeePrivacyNotice
from app.repositories.document import DocumentCategoryRepository, EmployeeDocumentRepository
from app.repositories.employee import EmployeeRepository
from app.repositories.onboarding import (
    AcknowledgementRepository,
    InstanceEventRepository,
    InstanceRepository,
    SubmissionRepository,
    TaskDependencyRepository,
    TaskDocumentRepository,
    TaskEventRepository,
    TaskRepository,
)
from app.repositories.organization import OrganizationRepository, OrganizationSettingsRepository
from app.repositories.platform import UserRepository
from app.services.access import AccessService
from app.services.approvals import employee_in_scope
from app.services.onboarding_rules import (
    CLOSED_OK,
    OPEN_STATUSES,
    TaskDefinitionError,
    can_transition,
    due_date,
    find_cycle,
    instance_phase,
    is_overdue,
    normalize_task,
    progress_percent,
)
from app.services.onboarding_templates import TemplateService
from app.services.organizations import assert_etag

ON_BEHALF_REASON_MIN = 10
OWNER_REVIEWER_REFS = (
    "owner_user_id", "owner_department_id", "reviewer_user_id", "reviewer_department_id"
)  # fmt: skip
_DEFINITION_COLUMNS = (
    "task_code", "title", "instructions", "phase", "task_type", "owner_type", "is_mandatory",
    "blocks_activation", "is_waivable", "review_required", "reviewer_type", "due_basis",
    "due_offset_days", "requires_document_verification", "min_documents", "data_section_code",
    "acknowledgement_kind", "policy_version_label", "privacy_notice_basis", "consent_scope",
    "acknowledgement_statement", "requires_evidence", "visible_to_employee", "display_order",
    "policy_file_id", "required_document_category_id",
)  # fmt: skip


@dataclass(slots=True)
class TaskState:
    task: OnboardingTask
    is_blocked: bool
    is_overdue: bool


@dataclass(slots=True)
class InstanceView:
    instance: OnboardingInstance
    phase: str
    tasks: list[TaskState]
    events: list[OnboardingInstanceEvent] = field(default_factory=list)
    mandatory_total: int = 0
    mandatory_closed: int = 0
    optional_total: int = 0
    optional_closed: int = 0
    overdue_count: int = 0
    progress_percent: int = 100
    warnings: list[Any] = field(default_factory=list)


class OnboardingService:
    def __init__(
        self,
        *,
        instances: InstanceRepository,
        tasks: TaskRepository,
        task_dependencies: TaskDependencyRepository,
        instance_events: InstanceEventRepository,
        task_events: TaskEventRepository,
        task_documents: TaskDocumentRepository,
        acknowledgements: AcknowledgementRepository,
        submissions: SubmissionRepository,
        templates: TemplateService,
        employees: EmployeeRepository,
        documents: EmployeeDocumentRepository,
        categories: DocumentCategoryRepository,
        users: UserRepository,
        access: AccessService,
        organizations: OrganizationRepository,
        settings: OrganizationSettingsRepository,
        audit: AuditWriter,
    ) -> None:
        self._instances = instances
        self._tasks = tasks
        self._task_deps = task_dependencies
        self._instance_events = instance_events
        self._task_events = task_events
        self._task_docs = task_documents
        self._acks = acknowledgements
        self._submissions = submissions
        self._templates = templates
        self._employees = employees
        self._documents = documents
        self._categories = categories
        self._users = users
        self._access = access
        self._organizations = organizations
        self._settings = settings
        self._audit = audit

    # Guards and helpers.

    def _settings_row(self, context: OrgContext) -> Any:
        return self._settings.require(context.organization_id)

    def _require_module(self, context: OrgContext) -> Any:
        row = self._settings_row(context)
        if not row.enable_onboarding_module:
            raise ModuleNotEnabled()
        return row

    def _today(self, context: OrgContext) -> date:
        organization = self._organizations.get(context.organization_id)
        return today_in(organization.timezone if organization else None)

    def own_employee_id(self, context: OrgContext) -> uuid.UUID:
        """11: self-service identity comes from the user link and the SELF grant."""
        context.require(Permission.SELF_ONBOARDING)
        settings = self._require_module(context)
        if not settings.enable_onboarding_self_service:
            raise SelfServiceNotEnabled()
        if context.employee_id is None:
            raise PermissionDenied("This account is not linked to an employee record.")
        return context.employee_id

    def _holds(
        self, user_id: uuid.UUID, organization_id: uuid.UUID, *permissions: Permission
    ) -> bool:
        try:
            resolved = self._access.resolve(user_id, organization_id)
        except OrgAccessDenied:
            return False
        return resolved.permissions.has_any(*permissions)

    def _active_user_for(
        self, employee_id: uuid.UUID, organization_id: uuid.UUID
    ) -> uuid.UUID | None:
        user = self._users.get_active_for_employee(employee_id)
        if user is not None and self._holds_any_grant(user.id, organization_id):
            return user.id
        return None

    def _holds_any_grant(self, user_id: uuid.UUID, organization_id: uuid.UUID) -> bool:
        try:
            self._access.resolve(user_id, organization_id)
        except OrgAccessDenied:
            return False
        return True

    def _event(
        self,
        context: OrgContext,
        instance: OnboardingInstance,
        event_type: str,
        *,
        from_status: str | None = None,
        to_status: str | None = None,
        reason: str | None = None,
        details: list[dict[str, Any]] | None = None,
    ) -> None:
        self._instance_events.add(
            OnboardingInstanceEvent(
                id=uuid7(),
                organization_id=context.organization_id,
                employee_id=instance.employee_id,
                instance_id=instance.id,
                event_type=event_type,
                from_status=from_status,
                to_status=to_status,
                details=details,
                reason=reason,
                actor_user_id=context.user_id,
                occurred_at=utc_now(),
                created_by=context.user_id,
            )
        )

    def _task_event(
        self,
        context: OrgContext,
        task: OnboardingTask,
        event_type: str,
        *,
        from_status: str | None = None,
        to_status: str | None = None,
        source: str = "USER",
        reason: str | None = None,
        on_behalf: bool = False,
        **extra: Any,
    ) -> None:
        self._task_events.add(
            OnboardingTaskEvent(
                id=uuid7(),
                organization_id=context.organization_id,
                instance_id=task.instance_id,
                task_id=task.id,
                event_type=event_type,
                from_status=from_status,
                to_status=to_status,
                source=source,
                reason=reason,
                acted_on_behalf=on_behalf,
                actor_user_id=context.user_id,
                occurred_at=utc_now(),
                created_by=context.user_id,
                **extra,
            )
        )

    # Assignee resolution (4.7).

    def _resolve_assignee(  # noqa: PLR0917
        self,
        context: OrgContext,
        employee: Employee,
        coordinator_id: uuid.UUID,
        owner_type: str,
        owner_user_id: uuid.UUID | None,
        owner_department_id: uuid.UUID | None,
    ) -> tuple[uuid.UUID | None, uuid.UUID | None, str]:
        org = context.organization_id
        if owner_type == "HR_COORDINATOR":
            return coordinator_id, None, "RESOLVED"
        if owner_type == "DEPARTMENT_QUEUE":
            return None, owner_department_id, "RESOLVED"
        if owner_type == "EMPLOYEE":
            user = self._active_user_for(employee.id, org)
            return (user, None, "RESOLVED") if user else (None, None, "AWAITING_EMPLOYEE_ACCESS")
        if owner_type == "REPORTING_MANAGER" and employee.reporting_manager_employee_id:
            user = self._active_user_for(employee.reporting_manager_employee_id, org)
            if user:
                return user, None, "RESOLVED"
        if owner_type == "SPECIFIC_USER" and owner_user_id:
            target = self._users.get(owner_user_id)
            if target is not None and target.is_active and self._holds_any_grant(target.id, org):
                return owner_user_id, None, "RESOLVED"
        return coordinator_id, None, "FALLBACK_TO_COORDINATOR"

    def _resolve_reviewer(
        self,
        coordinator_id: uuid.UUID,
        reviewer_type: str | None,
        user_id: uuid.UUID | None,
        dept: uuid.UUID | None,
    ) -> tuple[uuid.UUID | None, uuid.UUID | None]:
        if reviewer_type == "HR_COORDINATOR":
            return coordinator_id, None
        if reviewer_type == "SPECIFIC_USER":
            return user_id, None
        if reviewer_type == "DEPARTMENT_QUEUE":
            return None, dept
        return None, None

    # Initiation (4.2, 4.4, 4.5).

    def initiate(
        self,
        context: OrgContext,
        *,
        employee_id: uuid.UUID,
        template_id: uuid.UUID | None,
        hr_coordinator_user_id: uuid.UUID | None,
        target_completion_date: date | None,
        remarks: str | None,
        acknowledged: list[str],
    ) -> InstanceView:
        context.require(Permission.ONBOARDING_INITIATE)
        self._require_module(context)
        organization = self._organizations.get(context.organization_id)
        employee = self._employees.get_for_update(employee_id)  # lock 1: the employee
        if employee is None:
            raise ResourceNotFound("employee")
        if (
            employee.archived_at is not None
            or employee.status not in (EmployeeStatus.DRAFT.value, EmployeeStatus.ACTIVE.value)
            or (organization is not None and organization.status != "ACTIVE")
        ):
            raise OnboardingEmployeeNotEligible()
        if self._instances.open_for_employee(employee.id) is not None:
            raise OnboardingAlreadyExists()

        coordinator = hr_coordinator_user_id or context.user_id
        if not self._holds(coordinator, context.organization_id, Permission.ONBOARDING_MANAGE):
            raise ValidationFailed("The coordinator must hold onboarding.manage.").with_detail(
                field="hr_coordinator_user_id", code="INVALID", message="Choose another user."
            )
        selection = self._templates.select(context, employee, template_id=template_id)
        warnings = WarningCollector(acknowledged)
        warnings.add_if(selection.mismatch, WarningCode.ONB_TEMPLATE_MISMATCH)

        today = self._today(context)
        instance = OnboardingInstance(
            id=uuid7(),
            organization_id=context.organization_id,
            employee_id=employee.id,
            template_id=selection.template.id,
            template_version_id=selection.version.id,
            template_code_snapshot=selection.template.template_code,
            template_name_snapshot=selection.template.template_name,
            template_version_number=selection.version.version_number,
            selection_mode=selection.mode,
            status="IN_PROGRESS",
            initiated_on=today,
            hr_coordinator_user_id=coordinator,
            remarks=remarks,
            created_by=context.user_id,
        )
        self._instances.add(instance)
        try:
            self._instances.flush()
        except IntegrityError as error:
            if "uq_onboarding_instances_one_open" not in str(error.orig):
                raise
            raise OnboardingAlreadyExists() from error  # concurrent initiation (ONB-02)

        template_tasks, template_deps = self._templates.tasks_of(selection.version.id)
        created = self._snapshot_tasks(context, employee, instance, coordinator, template_tasks)
        by_template = {t.template_task_id: t for t in created}
        for dep in template_deps:
            self._task_deps.add(
                OnboardingTaskDependency(
                    id=uuid7(),
                    organization_id=context.organization_id,
                    instance_id=instance.id,
                    task_id=by_template[dep.task_id].id,
                    prerequisite_task_id=by_template[dep.prerequisite_task_id].id,
                    created_by=context.user_id,
                )
            )
        self._task_deps.flush()
        fallback = any(t.assignee_resolution == "FALLBACK_TO_COORDINATOR" for t in created)
        warnings.add_if(fallback, WarningCode.ONB_OWNER_FALLBACK)
        acknowledged_codes = warnings.enforce()

        mandatory_dues = [t.due_date for t in created if t.is_mandatory and t.due_date is not None]
        instance.target_completion_date = target_completion_date or max(
            mandatory_dues, default=None
        )
        self._event(context, instance, "INITIATED", to_status="IN_PROGRESS")
        self._audit.record_change(
            context,
            action=AuditAction.ONBOARDING_INITIATED,
            entity_type="onboarding_instance",
            entity_id=instance.id,
            employee_id=employee.id,
            after={"template": selection.template.template_code, "tasks": len(created)},
            acknowledged_warnings=acknowledged_codes,
        )
        self._instances.flush()  # the response must carry the final row_version
        view = self.view(context, instance)
        view.warnings = warnings.raised
        return view

    def _snapshot_tasks(
        self,
        context: OrgContext,
        employee: Employee,
        instance: OnboardingInstance,
        coordinator: uuid.UUID,
        template_tasks: list[OnboardingTemplateTask],
    ) -> list[OnboardingTask]:
        created: list[OnboardingTask] = []
        for source in template_tasks:
            created.append(self._new_task(context, employee, instance, coordinator, source=source))
        self._tasks.flush()
        return created

    def _new_task(
        self,
        context: OrgContext,
        employee: Employee,
        instance: OnboardingInstance,
        coordinator: uuid.UUID,
        *,
        source: OnboardingTemplateTask | None = None,
        values: dict[str, Any] | None = None,
    ) -> OnboardingTask:
        data = (
            {c: getattr(source, c) for c in _DEFINITION_COLUMNS}
            if source is not None
            else dict(values or {})
        )
        refs = {
            name: getattr(source, name, None) if source is not None else data.get(name)
            for name in OWNER_REVIEWER_REFS
        }
        for name in OWNER_REVIEWER_REFS:  # resolved below; not task columns of their own
            data.pop(name, None)
        owner_user, owner_dept = refs["owner_user_id"], refs["owner_department_id"]
        reviewer_user, reviewer_dept = refs["reviewer_user_id"], refs["reviewer_department_id"]
        assignee, assignee_dept, resolution = self._resolve_assignee(
            context, employee, coordinator, data["owner_type"], owner_user, owner_dept
        )
        reviewer, reviewer_dept_resolved = self._resolve_reviewer(
            coordinator, data.get("reviewer_type"), reviewer_user, reviewer_dept
        )
        category_code = None
        if data.get("required_document_category_id"):
            category = self._categories.require(
                data["required_document_category_id"], resource="document category"
            )
            category_code = category.category_code
        task = OnboardingTask(
            id=uuid7(),
            organization_id=context.organization_id,
            instance_id=instance.id,
            employee_id=employee.id,
            template_task_id=source.id if source else None,
            required_document_category_code_snapshot=category_code,
            assignee_user_id=assignee,
            assignee_department_id=assignee_dept,
            assignee_resolution=resolution,
            reviewer_user_id=reviewer,
            reviewer_department_id=reviewer_dept_resolved,
            due_date=due_date(
                data["due_basis"],
                data["due_offset_days"],
                joining_date=employee.joining_date,
                initiated_on=instance.initiated_on,
            ),
            status="NOT_STARTED",
            created_by=context.user_id,
            **data,
        )
        self._tasks.add(task)
        self._task_event(
            context, task, "CREATED", to_status="NOT_STARTED", source="INSTANCE_TRANSITION"
        )
        return task

    # Reads.

    def view(
        self, context: OrgContext, instance: OnboardingInstance, *, with_events: bool = False
    ) -> InstanceView:
        today = self._today(context)
        employee = self._employees.get(instance.employee_id)
        tasks = list(self._tasks.for_instance(instance.id))
        closed = {t.id for t in tasks if t.status in CLOSED_OK}
        prerequisites: dict[uuid.UUID, list[uuid.UUID]] = {}
        for dep in self._task_deps.for_instance(instance.id):
            prerequisites.setdefault(dep.task_id, []).append(dep.prerequisite_task_id)
        states = [
            TaskState(
                t,
                any(p not in closed for p in prerequisites.get(t.id, [])),
                is_overdue(t.status, t.due_date, today),
            )
            for t in tasks
        ]
        counted = [t for t in tasks if t.status != "CANCELLED"]
        mandatory = [t for t in counted if t.is_mandatory]
        view = InstanceView(
            instance=instance,
            phase=instance_phase(employee.joining_date if employee else None, today),
            tasks=states,
            events=list(self._instance_events.for_instance(instance.id)) if with_events else [],
            mandatory_total=len(mandatory),
            mandatory_closed=sum(1 for t in mandatory if t.status in CLOSED_OK),
            optional_total=len(counted) - len(mandatory),
            optional_closed=sum(1 for t in counted if not t.is_mandatory and t.status in CLOSED_OK),
            overdue_count=sum(1 for s in states if s.is_overdue),
        )
        view.progress_percent = progress_percent(view.mandatory_total, view.mandatory_closed)
        return view

    def _visible_employee(self, context: OrgContext, employee_id: uuid.UUID) -> Employee:
        employee = self._employees.get(employee_id)
        if employee is None:
            raise ResourceNotFound("employee")
        if context.is_self(employee_id) and context.has(Permission.SELF_ONBOARDING):
            return employee
        context.require(Permission.ONBOARDING_VIEW)
        if not employee_in_scope(context, self._employees, employee):
            raise ResourceNotFound("employee")
        return employee

    def get(self, context: OrgContext, instance_id: uuid.UUID) -> InstanceView:
        instance = self._instances.require(instance_id, resource="onboarding instance")
        self._visible_employee(context, instance.employee_id)
        return self.view(context, instance, with_events=True)

    def for_employee(
        self, context: OrgContext, employee_id: uuid.UUID, *, include_cancelled: bool
    ) -> list[InstanceView]:
        self._visible_employee(context, employee_id)
        return [
            self.view(context, i)
            for i in self._instances.for_employee(employee_id, include_cancelled=include_cancelled)
        ]

    def own_instance(self, context: OrgContext) -> InstanceView | None:
        employee_id = self.own_employee_id(context)
        instance = self._instances.open_for_employee(employee_id)
        return self.view(context, instance) if instance is not None else None

    def list_instances(
        self, context: OrgContext, *, statuses: list[str] | None, limit: int = 100
    ) -> list[InstanceView]:
        context.require(Permission.ONBOARDING_VIEW)
        employee_ids: list[uuid.UUID] | None = None
        if context.is_reporting_scoped:
            employee_ids = [*self._employees.descendant_ids(context.employee_id or uuid.uuid4())]
        statement = self._instances.list_query(statuses=statuses, employee_ids=employee_ids)
        rows = list(self._instances.session.execute(statement.limit(limit)).scalars())
        return [self.view(context, i) for i in rows]

    def task_queue(
        self, context: OrgContext, *, review: bool, statuses: list[str] | None
    ) -> list[OnboardingTask]:
        context.require_any(
            Permission.ONBOARDING_TASK_EXECUTE,
            Permission.ONBOARDING_TASK_REVIEW,
            Permission.ONBOARDING_MANAGE,
        )
        department = None
        if context.employee_id is not None:
            me = self._employees.get(context.employee_id)
            department = me.department_id if me else None
        return list(
            self._tasks.queue(
                user_id=context.user_id, department_id=department, statuses=statuses, review=review
            )
        )

    # Locking.

    def _lock_instance(
        self, instance_id: uuid.UUID
    ) -> tuple[Employee, OnboardingInstance, list[OnboardingTask]]:
        """Employee, then the instance, then its tasks ascending by id (7.3)."""
        instance = self._instances.require(instance_id, resource="onboarding instance")
        employee = self._employees.get_for_update(instance.employee_id)
        if employee is None:  # pragma: no cover - FK-backed
            raise ResourceNotFound("employee")
        self._instances.session.refresh(instance, with_for_update=True)
        return employee, instance, list(self._tasks.locked_for_instance(instance.id))

    def _assert_open(self, instance: OnboardingInstance) -> None:
        if instance.status != "IN_PROGRESS":
            raise OnboardingInstanceNotOpen()

    # Instance transitions (4.2).

    def update_instance(
        self,
        context: OrgContext,
        instance_id: uuid.UUID,
        *,
        changes: dict[str, Any],
        if_match: int | None,
    ) -> InstanceView:
        context.require(Permission.ONBOARDING_MANAGE)
        self._require_module(context)
        _employee, instance, _tasks = self._lock_instance(instance_id)
        assert_etag(instance.row_version, if_match)
        if "hr_coordinator_user_id" in changes and not self._holds(
            changes["hr_coordinator_user_id"], context.organization_id, Permission.ONBOARDING_MANAGE
        ):
            raise ValidationFailed("The coordinator must hold onboarding.manage.")
        if (target := changes.get("target_completion_date")) and target < instance.initiated_on:
            raise ValidationFailed("target_completion_date cannot precede the initiation date.")
        for name, event in (
            ("hr_coordinator_user_id", "COORDINATOR_CHANGED"),
            ("target_completion_date", "TARGET_DATE_CHANGED"),
        ):
            if name in changes and changes[name] != getattr(instance, name):
                self._event(
                    context,
                    instance,
                    event,
                    details=[
                        {
                            "field": name,
                            "before": str(getattr(instance, name)),
                            "after": str(changes[name]),
                        }
                    ],
                )
        for name, value in changes.items():
            setattr(instance, name, value)
        instance.updated_by = context.user_id
        self._audit.record_change(
            context,
            action=AuditAction.ONBOARDING_UPDATED,
            entity_type="onboarding_instance",
            entity_id=instance.id,
            employee_id=instance.employee_id,
            after={k: str(v) for k, v in changes.items() if k != "remarks"},
        )
        return self.view(context, instance)

    def complete(
        self, context: OrgContext, instance_id: uuid.UUID, *, if_match: int | None
    ) -> InstanceView:
        context.require(Permission.ONBOARDING_COMPLETE)
        self._require_module(context)
        employee, instance, tasks = self._lock_instance(instance_id)
        assert_etag(instance.row_version, if_match)
        self._finish_instance(context, employee, instance, tasks, mode="MANUAL")
        return self.view(context, instance)

    def _finish_instance(
        self,
        context: OrgContext,
        employee: Employee,
        instance: OnboardingInstance,
        tasks: list[OnboardingTask],
        *,
        mode: str,
    ) -> None:
        if instance.status != "IN_PROGRESS":
            raise InvalidOnboardingTransition()
        if employee.status != EmployeeStatus.ACTIVE.value:
            raise OnboardingEmployeeNotEligible(
                "The employee must be ACTIVE to complete onboarding."
            )
        open_mandatory = [
            t for t in tasks if t.is_mandatory and t.status not in CLOSED_OK | {"CANCELLED"}
        ]
        if open_mandatory:
            error = OnboardingMandatoryTasksIncomplete()
            for task in open_mandatory:
                error.with_detail(
                    field=task.task_code, code="OPEN", message=f"{task.title} is {task.status}."
                )
            raise error
        for task in tasks:
            if task.status in OPEN_STATUSES:  # open optional tasks
                self._cancel_task(context, task, "OPEN_AT_COMPLETION", source="INSTANCE_TRANSITION")
        instance.status = "COMPLETED"
        instance.completed_at = utc_now()
        instance.completed_by = context.user_id
        instance.completion_mode = mode
        instance.updated_by = context.user_id
        self._event(
            context,
            instance,
            "COMPLETED" if mode == "MANUAL" else "AUTO_COMPLETED",
            from_status="IN_PROGRESS",
            to_status="COMPLETED",
        )
        self._audit.record_change(
            context,
            action=AuditAction.ONBOARDING_COMPLETED,
            entity_type="onboarding_instance",
            entity_id=instance.id,
            employee_id=instance.employee_id,
            after={"completion_mode": mode},
        )

    def cancel(
        self,
        context: OrgContext,
        instance_id: uuid.UUID,
        *,
        reason: str,
        if_match: int | None,
        source: str = "USER",
    ) -> InstanceView:
        context.require(Permission.ONBOARDING_CANCEL)
        self._require_module(context)
        _employee, instance, tasks = self._lock_instance(instance_id)
        assert_etag(instance.row_version, if_match)
        if not 10 <= len(reason.strip()) <= 1000:
            raise ValidationFailed("A reason of 10 to 1,000 characters is required.")
        self._cancel_instance(context, instance, tasks, reason=reason.strip(), source=source)
        return self.view(context, instance)

    def _cancel_instance(
        self,
        context: OrgContext,
        instance: OnboardingInstance,
        tasks: list[OnboardingTask],
        *,
        reason: str,
        source: str,
    ) -> None:
        if instance.status != "IN_PROGRESS":
            raise InvalidOnboardingTransition()
        for task in tasks:
            if task.status in OPEN_STATUSES:
                self._cancel_task(context, task, "INSTANCE_CANCELLED", source="INSTANCE_TRANSITION")
        for submission in self._submissions_of_instance(instance, tasks):
            if submission.status == "SUBMITTED":
                submission.status = "WITHDRAWN"
        instance.status = "CANCELLED"
        instance.cancelled_at = utc_now()
        instance.cancelled_by = context.user_id
        instance.cancellation_reason = reason
        instance.cancellation_source = source
        instance.updated_by = context.user_id
        self._event(
            context,
            instance,
            "CANCELLED",
            from_status="IN_PROGRESS",
            to_status="CANCELLED",
            reason=reason,
        )
        self._audit.record_change(
            context,
            action=AuditAction.ONBOARDING_CANCELLED,
            entity_type="onboarding_instance",
            entity_id=instance.id,
            employee_id=instance.employee_id,
            reason=reason,
        )

    def _submissions_of_instance(
        self, instance: OnboardingInstance, tasks: list[OnboardingTask]
    ) -> list[Any]:
        rows: list[Any] = []
        for task in tasks:
            if task.task_type == "DATA_SUBMISSION":
                rows.extend(self._submissions.for_task(task.id))
        return rows

    def reopen(
        self, context: OrgContext, instance_id: uuid.UUID, *, reason: str, if_match: int | None
    ) -> InstanceView:
        context.require(Permission.ONBOARDING_REOPEN)
        settings = self._require_module(context)
        employee, instance, _tasks = self._lock_instance(instance_id)
        assert_etag(instance.row_version, if_match)
        if instance.status != "COMPLETED":
            raise InvalidOnboardingTransition()
        if len(reason.strip()) < 20:
            raise ValidationFailed("A reason of at least 20 characters is required.")
        window = timedelta(days=settings.onboarding_reopen_window_days)
        if (
            settings.onboarding_reopen_window_days == 0
            or instance.completed_at is None
            or utc_now() - instance.completed_at > window
        ):
            raise OnboardingReopenWindowExpired()
        if employee.status in {s.value for s in SEPARATED_STATUSES}:
            raise OnboardingEmployeeNotEligible()
        instance.status = "IN_PROGRESS"
        instance.completed_at = instance.completed_by = instance.completion_mode = None
        instance.reopened_count += 1
        instance.updated_by = context.user_id
        self._event(
            context,
            instance,
            "REOPENED",
            from_status="COMPLETED",
            to_status="IN_PROGRESS",
            reason=reason.strip(),
        )
        self._audit.record_change(
            context,
            action=AuditAction.ONBOARDING_REOPENED,
            entity_type="onboarding_instance",
            entity_id=instance.id,
            employee_id=instance.employee_id,
            reason=reason.strip(),
        )
        return self.view(context, instance)

    # Auto-completion and activation (4.9).

    def _maybe_auto_complete(
        self,
        context: OrgContext,
        employee: Employee,
        instance: OnboardingInstance,
        tasks: list[OnboardingTask],
    ) -> None:
        if (
            instance.status == "IN_PROGRESS"
            and self._settings_row(context).onboarding_auto_complete
            and employee.status == EmployeeStatus.ACTIVE.value
            and all(t.status in CLOSED_OK | {"CANCELLED"} for t in tasks if t.is_mandatory)
        ):
            self._finish_instance(context, employee, instance, tasks, mode="AUTOMATIC")

    def assert_can_activate(self, context: OrgContext, employee: Employee) -> None:
        """ONB-04 / 4.9: called by EMS activation with the employee row already locked."""
        settings = self._settings_row(context)
        if not settings.enable_onboarding_module:
            return
        instance = self._instances.open_for_employee(employee.id)
        if settings.onboarding_required_for_activation and instance is None:
            raise OnboardingRequiredForActivation()
        if instance is None or instance.status != "IN_PROGRESS":
            return
        blocking = [
            t
            for t in self._tasks.locked_for_instance(instance.id)
            if t.blocks_activation and t.status not in CLOSED_OK | {"CANCELLED"}
        ]
        if blocking:
            error = OnboardingActivationBlocked()
            error.details = [
                ErrorDetail(t.task_code, "BLOCKING", f"{t.title} is {t.status}.") for t in blocking
            ]
            raise error

    def after_activation(self, context: OrgContext, employee: Employee) -> None:
        if not self._settings_row(context).enable_onboarding_module:
            return
        instance = self._instances.open_for_employee(employee.id)
        if instance is not None and instance.status == "IN_PROGRESS":
            self._maybe_auto_complete(
                context, employee, instance, list(self._tasks.locked_for_instance(instance.id))
            )

    def cancel_for_employee(self, context: OrgContext, employee: Employee, *, source: str) -> None:
        """7.5: archive or separation cancels an IN_PROGRESS instance (system, no permission)."""
        if not self._settings_row(context).enable_onboarding_module:
            return
        instance = self._instances.open_for_employee(employee.id)
        if instance is None or instance.status != "IN_PROGRESS":
            return
        self._instances.session.refresh(instance, with_for_update=True)
        reason = (
            "Employee record archived." if source == "EMPLOYEE_ARCHIVED" else "Employee separated."
        )
        self._cancel_instance(
            context,
            instance,
            list(self._tasks.locked_for_instance(instance.id)),
            reason=reason,
            source=source,
        )

    def recompute_due_dates(self, context: OrgContext, employee: Employee) -> int:
        """4.7: a joining-date change recomputes open COMPUTED tasks with a JOINING_DATE basis."""
        if not self._settings_row(context).enable_onboarding_module:
            return 0
        instance = self._instances.open_for_employee(employee.id)
        if instance is None or instance.status != "IN_PROGRESS":
            return 0
        changed = 0
        for task in self._tasks.open_for_employee_with_joining_basis(employee.id):
            new = due_date(
                task.due_basis,
                task.due_offset_days,
                joining_date=employee.joining_date,
                initiated_on=instance.initiated_on,
            )
            if new != task.due_date:
                self._task_event(
                    context,
                    task,
                    "DUE_DATE_CHANGED",
                    source="JOINING_DATE_CHANGE",
                    due_date_before=task.due_date,
                    due_date_after=new,
                )
                task.due_date = new
                changed += 1
        if changed:
            self._event(context, instance, "DUE_DATES_RECALCULATED")
        return changed

    # Task actors and guards (4.7, 4.8).

    def _in_queue(self, context: OrgContext, department_id: uuid.UUID | None) -> bool:
        if department_id is None or not context.has(Permission.ONBOARDING_TASK_EXECUTE):
            return False
        me = self._employees.get(context.employee_id) if context.employee_id else None
        return me is not None and me.department_id == department_id

    def _acting(
        self,
        context: OrgContext,
        task: OnboardingTask,
        instance: OnboardingInstance,
        on_behalf_reason: str | None,
    ) -> bool:
        """True when HR acts on behalf of the assignee; raises 403 for anyone else."""
        self_owner = (
            task.owner_type == "EMPLOYEE"
            and context.is_self(task.employee_id)
            and context.has(Permission.SELF_ONBOARDING)
        )
        assignee = (
            task.owner_type != "EMPLOYEE"
            and context.has(Permission.ONBOARDING_TASK_EXECUTE)
            and (
                task.assignee_user_id == context.user_id  # noqa: PLR1714
                or instance.hr_coordinator_user_id == context.user_id
                or self._in_queue(context, task.assignee_department_id)
            )
        )
        if self_owner or assignee:
            return False
        if context.has(Permission.ONBOARDING_MANAGE):
            if len((on_behalf_reason or "").strip()) < ON_BEHALF_REASON_MIN:
                raise ValidationFailed(
                    "on_behalf_reason of at least 10 characters is required."
                ).with_detail(
                    field="on_behalf_reason",
                    code="REQUIRED",
                    message="Explain why you act for the assignee.",
                )
            return True
        raise PermissionDenied("You are not an assignee of this task.")

    def _is_blocked(self, task: OnboardingTask, siblings: list[OnboardingTask]) -> bool:
        status = {t.id: t.status for t in siblings}
        return any(
            status.get(dep.prerequisite_task_id) not in CLOSED_OK
            for dep in self._task_deps.for_instance(task.instance_id)
            if dep.task_id == task.id
        )

    def _load_task(
        self, instance_id: uuid.UUID, task_id: uuid.UUID
    ) -> tuple[Employee, OnboardingInstance, OnboardingTask, list[OnboardingTask]]:
        employee, instance, siblings = self._lock_instance(instance_id)
        task = next((t for t in siblings if t.id == task_id), None)
        if task is None:
            raise ResourceNotFound("onboarding task")
        return employee, instance, task, siblings

    def _move(
        self,
        context: OrgContext,
        task: OnboardingTask,
        target: str,
        *,
        source: str = "USER",
        reason: str | None = None,
        on_behalf: bool = False,
    ) -> None:
        if not can_transition(task.status, target):
            raise InvalidOnboardingTaskTransition()
        before = task.status
        task.status = target
        task.acted_on_behalf = on_behalf
        task.updated_by = context.user_id
        now = utc_now()
        if target == "IN_PROGRESS":
            task.started_at, task.started_by = (
                task.started_at or now,
                task.started_by or context.user_id,
            )
        elif target == "SUBMITTED":
            task.submitted_at, task.submitted_by = now, context.user_id
        elif target == "COMPLETED":
            task.completed_at, task.completed_by = now, context.user_id
        elif target == "WAIVED":
            task.waived_at, task.waived_by = now, context.user_id
        event = {
            "IN_PROGRESS": "REOPENED" if before in CLOSED_OK else "STARTED",
            "SUBMITTED": "SUBMITTED",
            "COMPLETED": "COMPLETED",
            "REJECTED": "REJECTED",
            "WAIVED": "WAIVED",
            "CANCELLED": "CANCELLED",
        }[target]
        self._task_event(
            context,
            task,
            event,
            from_status=before,
            to_status=target,
            source=source,
            reason=reason,
            on_behalf=on_behalf,
        )
        self._audit.record_change(
            context,
            action=AuditAction.ONBOARDING_TASK_STATUS_CHANGED,
            entity_type="onboarding_task",
            entity_id=task.id,
            employee_id=task.employee_id,
            before={"status": before},
            after={"status": target},
            reason=reason,
        )

    def _cancel_task(
        self, context: OrgContext, task: OnboardingTask, reason: str, *, source: str
    ) -> None:
        task.cancellation_reason = reason
        self._move(context, task, "CANCELLED", source=source, reason=reason)

    def _requirements(self, task: OnboardingTask) -> None:
        """Type completeness (4.8) for the user-completable types."""
        if task.task_type == "CHECKLIST" and task.requires_evidence:
            live = [
                link
                for link in self._task_docs.for_task(task.id, active_only=True)
                if self._available(link.document_id)
            ]
            if not live:
                raise OnboardingTaskRequirementsUnmet(
                    "At least one linked, available document is required."
                )

    def _available(self, document_id: uuid.UUID) -> bool:
        document = self._documents.get(document_id)
        return (
            document is not None and document.upload_status == "AVAILABLE" and document.is_current
        )

    def _after(
        self,
        context: OrgContext,
        employee: Employee,
        instance: OnboardingInstance,
        siblings: list[OnboardingTask],
    ) -> None:
        self._instances.session.flush()
        self._maybe_auto_complete(context, employee, instance, siblings)

    # Task actions (4.8).

    def start(
        self,
        context: OrgContext,
        instance_id: uuid.UUID,
        task_id: uuid.UUID,
        *,
        if_match: int | None,
        on_behalf_reason: str | None = None,
    ) -> OnboardingTask:
        _employee, instance, task, siblings = self._load_task(instance_id, task_id)
        self._assert_open(instance)
        assert_etag(task.row_version, if_match)
        behalf = self._acting(context, task, instance, on_behalf_reason)
        if task.status != "NOT_STARTED":
            raise InvalidOnboardingTaskTransition()
        if self._is_blocked(task, siblings):
            raise OnboardingTaskBlocked()
        self._move(
            context,
            task,
            "IN_PROGRESS",
            on_behalf=behalf,
            reason=on_behalf_reason if behalf else None,
        )
        return task

    def complete_task(
        self,
        context: OrgContext,
        instance_id: uuid.UUID,
        task_id: uuid.UUID,
        *,
        if_match: int | None,
        completion_remarks: str | None = None,
        on_behalf_reason: str | None = None,
    ) -> OnboardingTask:
        employee, instance, task, siblings = self._load_task(instance_id, task_id)
        self._assert_open(instance)
        assert_etag(task.row_version, if_match)
        behalf = self._acting(context, task, instance, on_behalf_reason)
        if (
            task.task_type != "CHECKLIST"
            or task.review_required
            or task.status not in {"NOT_STARTED", "IN_PROGRESS"}
        ):
            raise InvalidOnboardingTaskTransition("This task cannot be completed directly.")
        if self._is_blocked(task, siblings):
            raise OnboardingTaskBlocked()
        self._requirements(task)
        task.completion_remarks = completion_remarks
        self._move(
            context,
            task,
            "COMPLETED",
            on_behalf=behalf,
            reason=on_behalf_reason if behalf else None,
        )
        self._after(context, employee, instance, siblings)
        return task

    def submit_task(
        self,
        context: OrgContext,
        instance_id: uuid.UUID,
        task_id: uuid.UUID,
        *,
        if_match: int | None,
        resubmit: bool = False,
        on_behalf_reason: str | None = None,
    ) -> OnboardingTask:
        _employee, instance, task, siblings = self._load_task(instance_id, task_id)
        self._assert_open(instance)
        assert_etag(task.row_version, if_match)
        behalf = self._acting(context, task, instance, on_behalf_reason)
        expected = {"REJECTED"} if resubmit else {"NOT_STARTED", "IN_PROGRESS"}
        if task.task_type != "CHECKLIST" or not task.review_required or task.status not in expected:
            raise InvalidOnboardingTaskTransition("This task cannot be submitted directly.")
        if self._is_blocked(task, siblings):
            raise OnboardingTaskBlocked()
        self._requirements(task)
        self._move(
            context,
            task,
            "SUBMITTED",
            on_behalf=behalf,
            reason=on_behalf_reason if behalf else None,
        )
        return task

    def _reviewer_check(
        self, context: OrgContext, task: OnboardingTask, instance: OnboardingInstance
    ) -> None:
        context.require(Permission.ONBOARDING_TASK_REVIEW)
        if task.submitted_by == context.user_id:
            raise SelfReviewNotAllowed()
        allowed = (
            task.reviewer_user_id == context.user_id  # noqa: PLR1714
            or instance.hr_coordinator_user_id == context.user_id
            or self._in_queue(context, task.reviewer_department_id)
            or context.has(Permission.ONBOARDING_MANAGE)
        )
        if not allowed:
            raise PermissionDenied("You are not a reviewer of this task.")

    def accept_task(
        self,
        context: OrgContext,
        instance_id: uuid.UUID,
        task_id: uuid.UUID,
        *,
        if_match: int | None,
    ) -> OnboardingTask:
        employee, instance, task, siblings = self._load_task(instance_id, task_id)
        self._assert_open(instance)
        assert_etag(task.row_version, if_match)
        self._reviewer_check(context, task, instance)
        if task.task_type == "DATA_SUBMISSION" or task.status != "SUBMITTED":
            raise InvalidOnboardingTaskTransition(
                "Data submissions are accepted through the submission."
            )
        task.reviewed_at, task.reviewed_by = utc_now(), context.user_id
        self._move(context, task, "COMPLETED")
        self._after(context, employee, instance, siblings)
        return task

    def reject_task(
        self,
        context: OrgContext,
        instance_id: uuid.UUID,
        task_id: uuid.UUID,
        *,
        reason: str,
        if_match: int | None,
    ) -> OnboardingTask:
        _employee, instance, task, _siblings = self._load_task(instance_id, task_id)
        self._assert_open(instance)
        assert_etag(task.row_version, if_match)
        self._reviewer_check(context, task, instance)
        if not 10 <= len(reason.strip()) <= 1000:
            raise ValidationFailed("A rejection reason of 10 to 1,000 characters is required.")
        if task.status != "SUBMITTED":
            raise InvalidOnboardingTaskTransition()
        task.rejection_reason = reason.strip()
        task.reviewed_at, task.reviewed_by = utc_now(), context.user_id
        self._move(context, task, "REJECTED", reason=reason.strip())
        return task

    def waive(
        self,
        context: OrgContext,
        instance_id: uuid.UUID,
        task_id: uuid.UUID,
        *,
        reason: str,
        if_match: int | None,
    ) -> OnboardingTask:
        context.require(Permission.ONBOARDING_TASK_WAIVE)
        employee, instance, task, siblings = self._load_task(instance_id, task_id)
        self._assert_open(instance)
        assert_etag(task.row_version, if_match)
        if not task.is_waivable:
            raise TaskNotWaivable()
        if len(reason.strip()) < 20:
            raise ValidationFailed("A waiver reason of at least 20 characters is required.")
        task.waiver_reason = reason.strip()
        self._move(context, task, "WAIVED", reason=reason.strip())
        self._after(context, employee, instance, siblings)
        return task

    def reopen_task(
        self,
        context: OrgContext,
        instance_id: uuid.UUID,
        task_id: uuid.UUID,
        *,
        reason: str,
        if_match: int | None,
        acknowledged: list[str],
    ) -> OnboardingTask:
        context.require(Permission.ONBOARDING_MANAGE)
        employee, instance, task, _siblings = self._load_task(instance_id, task_id)
        self._assert_open(instance)
        assert_etag(task.row_version, if_match)
        if len(reason.strip()) < 10:
            raise ValidationFailed("A reason of at least 10 characters is required.")
        warnings = WarningCollector(acknowledged)
        warnings.add_if(
            task.blocks_activation and employee.status == EmployeeStatus.ACTIVE.value,
            WarningCode.ONB_REOPEN_AFTER_ACTIVATION,
        )
        warnings.enforce()
        if task.status not in CLOSED_OK:
            raise InvalidOnboardingTaskTransition()
        task.reopen_count += 1
        self._move(context, task, "IN_PROGRESS", reason=reason.strip())
        return task

    def reassign(
        self,
        context: OrgContext,
        instance_id: uuid.UUID,
        task_id: uuid.UUID,
        *,
        assignee_user_id: uuid.UUID | None,
        assignee_department_id: uuid.UUID | None,
        reason: str,
        if_match: int | None,
    ) -> OnboardingTask:
        context.require(Permission.ONBOARDING_MANAGE)
        _employee, instance, task, _siblings = self._load_task(instance_id, task_id)
        self._assert_open(instance)
        assert_etag(task.row_version, if_match)
        if task.owner_type == "EMPLOYEE":
            raise ValidationFailed(
                "EMPLOYEE-owned tasks are not reassigned; act on behalf instead."
            )
        if (assignee_user_id is None) == (assignee_department_id is None):
            raise ValidationFailed(
                "Provide exactly one of assignee_user_id or assignee_department_id."
            )
        if len(reason.strip()) < 10:
            raise ValidationFailed("A reason of at least 10 characters is required.")
        if assignee_user_id is not None and not self._holds(
            assignee_user_id,
            context.organization_id,
            Permission.ONBOARDING_TASK_EXECUTE,
            Permission.ONBOARDING_MANAGE,
        ):
            raise ValidationFailed("The assignee cannot execute onboarding tasks.")
        self._task_event(
            context,
            task,
            "REASSIGNED",
            reason=reason.strip(),
            from_assignee_user_id=task.assignee_user_id,
            to_assignee_user_id=assignee_user_id,
            from_assignee_department_id=task.assignee_department_id,
            to_assignee_department_id=assignee_department_id,
        )
        task.assignee_user_id, task.assignee_department_id = (
            assignee_user_id,
            assignee_department_id,
        )
        task.assignee_resolution = "RESOLVED"
        task.updated_by = context.user_id
        self._audit.record_change(
            context,
            action=AuditAction.ONBOARDING_TASK_REASSIGNED,
            entity_type="onboarding_task",
            entity_id=task.id,
            employee_id=task.employee_id,
            reason=reason.strip(),
        )
        return task

    def set_due_date(
        self,
        context: OrgContext,
        instance_id: uuid.UUID,
        task_id: uuid.UUID,
        *,
        new_date: date,
        reason: str,
        if_match: int | None,
    ) -> OnboardingTask:
        context.require(Permission.ONBOARDING_MANAGE)
        _employee, instance, task, _siblings = self._load_task(instance_id, task_id)
        self._assert_open(instance)
        assert_etag(task.row_version, if_match)
        if len(reason.strip()) < 10:
            raise ValidationFailed("A reason of at least 10 characters is required.")
        self._task_event(
            context,
            task,
            "DUE_DATE_CHANGED",
            reason=reason.strip(),
            due_date_before=task.due_date,
            due_date_after=new_date,
        )
        task.due_date, task.due_date_source = new_date, "MANUAL"
        task.updated_by = context.user_id
        self._audit.record_change(
            context,
            action=AuditAction.ONBOARDING_TASK_DUE_DATE_CHANGED,
            entity_type="onboarding_task",
            entity_id=task.id,
            employee_id=task.employee_id,
            reason=reason.strip(),
        )
        return task

    def remove_adhoc_task(
        self,
        context: OrgContext,
        instance_id: uuid.UUID,
        task_id: uuid.UUID,
        *,
        reason: str,
        if_match: int | None,
    ) -> OnboardingTask:
        context.require(Permission.ONBOARDING_MANAGE)
        _employee, instance, task, _siblings = self._load_task(instance_id, task_id)
        self._assert_open(instance)
        assert_etag(task.row_version, if_match)
        if task.template_task_id is not None or task.status != "NOT_STARTED":
            raise InvalidOnboardingTaskTransition(
                "Only an ad-hoc task that has not started can be removed."
            )
        if len(reason.strip()) < 10:
            raise ValidationFailed("A reason of at least 10 characters is required.")
        self._cancel_task(context, task, reason.strip(), source="USER")
        return task

    def add_task(
        self,
        context: OrgContext,
        instance_id: uuid.UUID,
        *,
        values: dict[str, Any],
        prerequisite_ids: list[uuid.UUID],
    ) -> OnboardingTask:
        """An ad-hoc task (4.5): the template is never edited retroactively."""
        context.require(Permission.ONBOARDING_MANAGE)
        self._require_module(context)
        employee, instance, siblings = self._lock_instance(instance_id)
        self._assert_open(instance)
        if any(t.task_code == values["task_code"].upper() for t in siblings):
            raise ValidationFailed("That task_code already exists on the instance.")
        category = (
            self._categories.require(
                values["required_document_category_id"], resource="document category"
            )
            if values.get("required_document_category_id")
            else None
        )
        try:
            normalized = normalize_task(
                values,
                category_requires_verification=category.requires_verification if category else None,
            )
        except TaskDefinitionError as error:
            invalid = OnboardingTemplateInvalid("The task definition is invalid.")
            for name, message in error.problems:
                invalid.with_detail(field=name, code="INVALID", message=message)
            raise invalid from error
        normalized["task_code"] = values["task_code"].upper()
        task = self._new_task(
            context,
            employee,
            instance,
            instance.hr_coordinator_user_id,
            values={
                **normalized,
                **{k: v for k, v in values.items() if k.startswith(("owner_", "reviewer_"))},
            },
        )
        self._tasks.flush()
        existing = self._task_deps.for_instance(instance.id)
        edges = [(str(d.task_id), str(d.prerequisite_task_id)) for d in existing]
        known = {t.id for t in siblings}
        for prerequisite in prerequisite_ids:
            if prerequisite not in known:
                raise ValidationFailed("A prerequisite does not belong to this instance.")
            edges.append((str(task.id), str(prerequisite)))
        if find_cycle(edges) is not None:
            raise ValidationFailed("The dependencies would create a cycle.")
        for prerequisite in set(prerequisite_ids):
            self._task_deps.add(
                OnboardingTaskDependency(
                    id=uuid7(),
                    organization_id=context.organization_id,
                    instance_id=instance.id,
                    task_id=task.id,
                    prerequisite_task_id=prerequisite,
                    created_by=context.user_id,
                )
            )
        self._event(
            context,
            instance,
            "TASK_ADDED",
            details=[{"field": "task_code", "before": "", "after": task.task_code}],
        )
        self._audit.record_change(
            context,
            action=AuditAction.ONBOARDING_TASK_ADDED,
            entity_type="onboarding_task",
            entity_id=task.id,
            employee_id=employee.id,
            after={"task_code": task.task_code},
        )
        return task

    # Documents (4.11).

    def link_document(
        self,
        context: OrgContext,
        instance_id: uuid.UUID,
        task_id: uuid.UUID,
        *,
        document_id: uuid.UUID,
    ) -> OnboardingTaskDocument:
        context.require(Permission.ONBOARDING_MANAGE)
        employee, instance, task, siblings = self._load_task(instance_id, task_id)
        self._assert_open(instance)
        if task.status in {"COMPLETED", "WAIVED", "CANCELLED"}:
            raise InvalidOnboardingTaskTransition()
        document = self._documents.get(document_id)
        if document is None or document.employee_id != task.employee_id:
            raise ResourceNotFound("document")
        category = self._categories.require(
            document.document_category_id, resource="document category"
        )
        if not self._available(document.id):
            raise DocumentTaskMismatch("The document is not a current, available version.")
        if task.task_type == "DOCUMENT_UPLOAD":
            if document.document_category_id != task.required_document_category_id:
                raise DocumentTaskMismatch()
        elif task.task_type == "CHECKLIST" and task.requires_evidence:
            if category.is_restricted and category.category_code != "ONBOARDING_TASK_EVIDENCE":
                raise DocumentTaskMismatch()
        else:
            raise DocumentTaskMismatch("This task type takes no documents.")
        link = self._task_docs.find(task.id, document.id)
        if link is not None and link.link_status == "ACTIVE":
            return link
        if link is None:
            link = OnboardingTaskDocument(
                id=uuid7(),
                organization_id=context.organization_id,
                instance_id=instance.id,
                task_id=task.id,
                employee_id=task.employee_id,
                document_id=document.id,
                link_status="ACTIVE",
                linked_by=context.user_id,
                linked_at=utc_now(),
                created_by=context.user_id,
            )
            self._task_docs.add(link)
        else:
            link.link_status, link.unlinked_at, link.unlinked_by, link.unlink_reason = (
                "ACTIVE",
                None,
                None,
                None,
            )
        self._task_docs.flush()
        self._task_event(context, task, "DOCUMENT_LINKED", related_document_id=document.id)
        self._audit.record_change(
            context,
            action=AuditAction.ONBOARDING_TASK_DOCUMENT_LINKED,
            entity_type="onboarding_task",
            entity_id=task.id,
            employee_id=task.employee_id,
            after={"document_id": str(document.id)},
        )
        self._evaluate_document_task(context, employee, instance, task, siblings)
        return link

    def unlink_document(
        self,
        context: OrgContext,
        instance_id: uuid.UUID,
        task_id: uuid.UUID,
        link_id: uuid.UUID,
        *,
        reason: str,
    ) -> OnboardingTaskDocument:
        context.require(Permission.ONBOARDING_MANAGE)
        _employee, instance, task, _siblings = self._load_task(instance_id, task_id)
        self._assert_open(instance)
        if task.status == "COMPLETED":
            raise InvalidOnboardingTaskTransition("Reopen a completed task before unlinking.")
        link = self._task_docs.require(link_id, resource="document link")
        if link.task_id != task.id or link.link_status != "ACTIVE":
            raise ResourceNotFound("document link")
        if len(reason.strip()) < 10:
            raise ValidationFailed("A reason of at least 10 characters is required.")
        link.link_status, link.unlinked_at, link.unlinked_by = (
            "UNLINKED",
            utc_now(),
            context.user_id,
        )
        link.unlink_reason = reason.strip()
        self._task_event(
            context,
            task,
            "DOCUMENT_UNLINKED",
            related_document_id=link.document_id,
            reason=reason.strip(),
        )
        self._audit.record_change(
            context,
            action=AuditAction.ONBOARDING_TASK_DOCUMENT_UNLINKED,
            entity_type="onboarding_task",
            entity_id=task.id,
            employee_id=task.employee_id,
            reason=reason.strip(),
        )
        return link

    def _evaluate_document_task(
        self,
        context: OrgContext,
        employee: Employee,
        instance: OnboardingInstance,
        task: OnboardingTask,
        siblings: list[OnboardingTask],
    ) -> None:
        """4.11: enough live links move a DOCUMENT_UPLOAD task forward (system transition)."""
        if task.task_type != "DOCUMENT_UPLOAD" or task.status not in {
            "NOT_STARTED",
            "IN_PROGRESS",
            "REJECTED",
        }:
            return
        live = [
            lk
            for lk in self._task_docs.for_task(task.id, active_only=True)
            if self._available(lk.document_id)
        ]
        if len(live) < (task.min_documents or 1):
            return
        if task.requires_document_verification:
            self._move(context, task, "SUBMITTED", source="DOCUMENT_VERIFICATION")
        else:
            self._move(context, task, "COMPLETED", source="USER")
            self._after(context, employee, instance, siblings)

    def on_document_verified(
        self,
        context: OrgContext,
        document_id: uuid.UUID,
        *,
        decision: str,
        rejection_reason: str | None,
    ) -> None:
        """4.11: called by EMS document verification, in the verifier's transaction."""
        if not self._settings_row(context).enable_onboarding_module:
            return
        for link in self._task_docs.active_for_document(document_id):
            employee, instance, task, siblings = self._load_task(link.instance_id, link.task_id)
            if (
                instance.status != "IN_PROGRESS"
                or task.task_type != "DOCUMENT_UPLOAD"
                or not task.requires_document_verification
            ):
                continue
            if decision == "VERIFIED" and task.status == "SUBMITTED":
                verified = [
                    lk
                    for lk in self._task_docs.for_task(task.id, active_only=True)
                    if (d := self._documents.get(lk.document_id)) is not None
                    and d.verification_status == "VERIFIED"
                    and self._available(lk.document_id)
                ]
                if len(verified) >= (task.min_documents or 1):
                    self._move(context, task, "COMPLETED", source="DOCUMENT_VERIFICATION")
                    self._after(context, employee, instance, siblings)
            elif decision == "REJECTED" and task.status == "SUBMITTED":
                task.rejection_reason = rejection_reason or "Document rejected."
                self._move(
                    context,
                    task,
                    "REJECTED",
                    source="DOCUMENT_VERIFICATION",
                    reason=task.rejection_reason,
                )

    # Acknowledgements (4.12).

    def acknowledge(
        self,
        context: OrgContext,
        instance_id: uuid.UUID,
        task_id: uuid.UUID,
        *,
        paper: bool,
        supporting_document_id: uuid.UUID | None = None,
        delivered_at: datetime | None = None,
        on_behalf_reason: str | None = None,
    ) -> OnboardingAcknowledgement:
        employee, instance, task, siblings = self._load_task(instance_id, task_id)
        self._assert_open(instance)
        if task.task_type != "ACKNOWLEDGEMENT" or task.status not in {
            "NOT_STARTED",
            "IN_PROGRESS",
            "REJECTED",
        }:
            raise InvalidOnboardingTaskTransition()
        if self._is_blocked(task, siblings):
            raise OnboardingTaskBlocked()
        now = utc_now()
        if paper:
            context.require(Permission.ONBOARDING_MANAGE)
            if (
                supporting_document_id is None
                or len((on_behalf_reason or "").strip()) < ON_BEHALF_REASON_MIN
            ):
                raise ValidationFailed("A signed document and an on_behalf_reason are required.")
            document = self._documents.get(supporting_document_id)
            if document is None or document.employee_id != task.employee_id:
                raise ResourceNotFound("document")
            delivered = delivered_at or now
            if delivered > now:
                raise ValidationFailed("delivered_at cannot be in the future.")
        else:
            # Electronic acknowledgement is the linked employee's alone; HR may not click for them.
            if not (context.is_self(task.employee_id) and context.has(Permission.SELF_ONBOARDING)):
                raise ValidationFailed(
                    "An electronic acknowledgement can only be made by the employee."
                )
            delivered = now
        notice_id: uuid.UUID | None = None
        if task.acknowledgement_kind == "PRIVACY_NOTICE":
            notice = EmployeePrivacyNotice(
                id=uuid7(),
                organization_id=context.organization_id,
                employee_id=task.employee_id,
                notice_version=task.policy_version_label,
                basis=task.privacy_notice_basis,
                consent_scope=task.consent_scope,
                delivered_at=delivered,
                acknowledged_at=now,
                method="PAPER_UPLOADED" if paper else "ELECTRONIC",
                document_id=supporting_document_id if paper else None,
                created_by=context.user_id,
            )
            self._acks.session.add(notice)
            self._acks.flush()
            notice_id = notice.id
        row = OnboardingAcknowledgement(
            id=uuid7(),
            organization_id=context.organization_id,
            employee_id=task.employee_id,
            instance_id=instance.id,
            task_id=task.id,
            acknowledgement_kind=task.acknowledgement_kind,
            policy_file_id=task.policy_file_id,
            policy_version_label=task.policy_version_label,
            statement_snapshot=task.acknowledgement_statement,
            method="PAPER_UPLOADED" if paper else "ELECTRONIC",
            supporting_document_id=supporting_document_id if paper else None,
            delivered_at=delivered,
            acknowledged_by_user_id=context.user_id,
            acknowledged_on_behalf=paper,
            on_behalf_reason=on_behalf_reason if paper else None,
            privacy_notice_id=notice_id,
            acknowledged_at=now,
            created_by=context.user_id,
        )
        self._acks.add(row)
        self._acks.flush()
        self._move(
            context,
            task,
            "SUBMITTED" if task.review_required else "COMPLETED",
            source="ACKNOWLEDGEMENT",
            on_behalf=paper,
        )
        self._audit.record_change(
            context,
            action=AuditAction.ONBOARDING_ACKNOWLEDGED,
            entity_type="onboarding_task",
            entity_id=task.id,
            employee_id=task.employee_id,
            after={"method": row.method, "policy_version": row.policy_version_label},
        )
        self._after(context, employee, instance, siblings)
        return row

    # Reads of a single task.

    def get_task(
        self, context: OrgContext, instance_id: uuid.UUID, task_id: uuid.UUID
    ) -> tuple[OnboardingTask, list[OnboardingTaskEvent], list[OnboardingTaskDocument]]:
        instance = self._instances.require(instance_id, resource="onboarding instance")
        task = self._tasks.require(task_id, resource="onboarding task")
        if task.instance_id != instance.id:
            raise ResourceNotFound("onboarding task")
        actor = (
            task.assignee_user_id == context.user_id  # noqa: PLR1714
            or task.reviewer_user_id == context.user_id
            or instance.hr_coordinator_user_id == context.user_id
        )
        if not actor:
            self._visible_employee(context, instance.employee_id)
        return (
            task,
            list(self._task_events.for_task(task.id)),
            list(self._task_docs.for_task(task.id)),
        )

    def own_task_instance(self, employee_id: uuid.UUID, task_id: uuid.UUID) -> uuid.UUID:
        """Self-service: the instance of one of the caller's own tasks (404 for anyone else's)."""
        task = self._tasks.get(task_id)
        if task is None or task.employee_id != employee_id:
            raise ResourceNotFound("onboarding task")
        return task.instance_id

    # Public names for the collaborating submission service (same module family).
    visible_employee = _visible_employee
    load_task = _load_task
    assert_open = _assert_open
    acting = _acting
    is_blocked = _is_blocked
    move = _move
    after = _after
    reviewer_check = _reviewer_check
