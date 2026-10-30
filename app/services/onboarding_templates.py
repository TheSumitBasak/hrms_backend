"""Onboarding templates, versions and selection (addendum 4.3, 4.4)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from app.audit.actions import AuditAction
from app.audit.writer import AuditWriter
from app.core.clock import today_in, utc_now
from app.core.context import OrgContext
from app.core.errors import (
    DuplicateResource,
    InvalidStatusTransition,
    ModuleNotEnabled,
    NoApplicableOnboardingTemplate,
    OnboardingTemplateConflict,
    OnboardingTemplateDraftExists,
    OnboardingTemplateInvalid,
    OnboardingTemplateVersionLocked,
    ValidationFailed,
)
from app.core.permissions import Permission
from app.db.types import uuid7
from app.models.employee import Employee
from app.models.onboarding import (
    APPLIES,
    OnboardingTemplate,
    OnboardingTemplateTask,
    OnboardingTemplateTaskDependency,
    OnboardingTemplateVersion,
)
from app.repositories.document import DocumentCategoryRepository
from app.repositories.onboarding import (
    TemplateDependencyRepository,
    TemplateRepository,
    TemplateTaskRepository,
    TemplateVersionRepository,
)
from app.repositories.organization import OrganizationRepository, OrganizationSettingsRepository
from app.services.onboarding_rules import TaskDefinitionError, find_cycle, normalize_task
from app.services.organizations import assert_etag
from app.services.versioning import intervals_overlap

_APPLIES = tuple(f"applies_{name}_id" for name, _ in APPLIES)
_EMPLOYEE_ATTRIBUTES = tuple(f"{name}_id" for name, _ in APPLIES)
VERSION_FIELDS = frozenset({"effective_from", "priority", *_APPLIES, "change_note"})


@dataclass(frozen=True, slots=True)
class Selection:
    version: OnboardingTemplateVersion
    template: OnboardingTemplate
    mismatch: bool
    mode: str  # AUTOMATIC | EXPLICIT
    candidates: list[tuple[OnboardingTemplateVersion, OnboardingTemplate]]


class TemplateService:
    def __init__(
        self,
        *,
        templates: TemplateRepository,
        versions: TemplateVersionRepository,
        tasks: TemplateTaskRepository,
        dependencies: TemplateDependencyRepository,
        categories: DocumentCategoryRepository,
        organizations: OrganizationRepository,
        settings: OrganizationSettingsRepository,
        audit: AuditWriter,
    ) -> None:
        self._templates = templates
        self._versions = versions
        self._tasks = tasks
        self._deps = dependencies
        self._categories = categories
        self._organizations = organizations
        self._settings = settings
        self._audit = audit

    # Guards.

    def _manage(self, context: OrgContext) -> None:
        context.require(Permission.ONBOARDING_TEMPLATE_MANAGE)
        if not self._settings.require(context.organization_id).enable_onboarding_module:
            raise ModuleNotEnabled()

    def _today(self, context: OrgContext) -> date:
        organization = self._organizations.get(context.organization_id)
        return today_in(organization.timezone if organization else None)

    # Reads.

    def list_templates(
        self, context: OrgContext, *, status: str | None
    ) -> tuple[list[OnboardingTemplate], dict[uuid.UUID, list[OnboardingTemplateVersion]]]:
        context.require_any(Permission.ONBOARDING_TEMPLATE_MANAGE, Permission.ONBOARDING_INITIATE)
        rows = list(self._templates.list_templates(status=status))
        return rows, self._versions.for_templates([r.id for r in rows])

    def get_template(
        self, context: OrgContext, template_id: uuid.UUID
    ) -> tuple[OnboardingTemplate, list[OnboardingTemplateVersion]]:
        context.require_any(Permission.ONBOARDING_TEMPLATE_MANAGE, Permission.ONBOARDING_INITIATE)
        template = self._templates.require(template_id, resource="onboarding template")
        return template, self._versions.for_templates([template.id])[template.id]

    def get_version(
        self, context: OrgContext, template_id: uuid.UUID, version_id: uuid.UUID
    ) -> tuple[OnboardingTemplateVersion, list[OnboardingTemplateTask], dict[uuid.UUID, str]]:
        context.require_any(Permission.ONBOARDING_TEMPLATE_MANAGE, Permission.ONBOARDING_INITIATE)
        version = self._version(template_id, version_id)
        tasks = list(self._tasks.for_version(version.id))
        codes = {t.id: t.task_code for t in tasks}
        return version, tasks, codes

    def dependencies_of(self, version_id: uuid.UUID) -> list[OnboardingTemplateTaskDependency]:
        return list(self._deps.for_version(version_id))

    def _version(self, template_id: uuid.UUID, version_id: uuid.UUID) -> OnboardingTemplateVersion:
        version = self._versions.require(version_id, resource="template version")
        if version.template_id != template_id:
            raise ValidationFailed("That version does not belong to the template.")
        return version

    # Writes.

    def create_template(
        self,
        context: OrgContext,
        *,
        values: dict[str, Any],
        version: dict[str, Any],
        tasks: list[dict[str, Any]],
        dependencies: list[dict[str, str]],
    ) -> OnboardingTemplate:
        self._manage(context)
        if self._templates.code_exists(values["template_code"]):
            raise DuplicateResource("An onboarding template with that code already exists.")
        template = OnboardingTemplate(
            id=uuid7(),
            organization_id=context.organization_id,
            created_by=context.user_id,
            **values,
        )
        self._templates.add(template)
        self._templates.flush()
        draft = self._new_version(context, template, number=1, fields=version)
        self._write_tasks(context, draft, tasks, dependencies)
        self._audit.record_change(
            context,
            action=AuditAction.ONBOARDING_TEMPLATE_CREATED,
            entity_type="onboarding_template",
            entity_id=template.id,
            after={"template_code": template.template_code},
        )
        return template

    def update_template(
        self,
        context: OrgContext,
        *,
        template_id: uuid.UUID,
        changes: dict[str, Any],
        if_match: int | None,
    ) -> OnboardingTemplate:
        self._manage(context)
        template = self._templates.require(template_id, resource="onboarding template")
        assert_etag(template.row_version, if_match)
        for field, value in changes.items():
            setattr(template, field, value)
        template.updated_by = context.user_id
        self._audit.record_change(
            context,
            action=AuditAction.ONBOARDING_TEMPLATE_UPDATED,
            entity_type="onboarding_template",
            entity_id=template.id,
            after=dict(changes),
        )
        return template

    def set_status(
        self, context: OrgContext, *, template_id: uuid.UUID, status: str, reason: str
    ) -> OnboardingTemplate:
        self._manage(context)
        template = self._templates.require(template_id, resource="onboarding template")
        if template.status == status:
            raise InvalidStatusTransition(f"The template is already {status}.")
        before = template.status
        template.status = status
        template.updated_by = context.user_id
        self._audit.record_change(
            context,
            action=AuditAction.ONBOARDING_TEMPLATE_STATUS_CHANGED,
            entity_type="onboarding_template",
            entity_id=template.id,
            before={"status": before},
            after={"status": status},
            reason=reason,
        )
        return template

    def _new_version(
        self,
        context: OrgContext,
        template: OnboardingTemplate,
        *,
        number: int,
        fields: dict[str, Any],
    ) -> OnboardingTemplateVersion:
        unknown = set(fields) - VERSION_FIELDS
        if unknown:
            raise ValidationFailed(f"Unknown version fields: {', '.join(sorted(unknown))}.")
        version = OnboardingTemplateVersion(
            id=uuid7(),
            organization_id=context.organization_id,
            template_id=template.id,
            version_number=number,
            status="DRAFT",
            created_by=context.user_id,
            **fields,
        )
        self._versions.add(version)
        self._versions.flush()
        return version

    def new_version(self, context: OrgContext, template_id: uuid.UUID) -> OnboardingTemplateVersion:
        """A DRAFT copied from the latest version (tasks and dependencies included)."""
        self._manage(context)
        template = self._templates.lock(template_id)
        existing = self._versions.for_templates([template.id])[template.id]
        if any(v.status == "DRAFT" for v in existing):
            raise OnboardingTemplateDraftExists()
        latest = max(existing, key=lambda v: v.version_number)
        fields = {
            f: getattr(latest, f)
            for f in VERSION_FIELDS
            if f not in ("effective_from", "change_note")
        }
        draft = self._new_version(
            context, template, number=latest.version_number + 1, fields=fields
        )
        sources = list(self._tasks.for_version(latest.id))
        copies: dict[uuid.UUID, OnboardingTemplateTask] = {}
        for source in sources:
            data = {
                c.name: getattr(source, c.name)
                for c in OnboardingTemplateTask.__table__.columns
                if c.name
                not in {
                    "id",
                    "template_version_id",
                    "created_at",
                    "created_by",
                    "updated_at",
                    "updated_by",
                    "row_version",
                    "archived_at",
                    "archived_by",
                    "archive_reason",
                    "organization_id",
                }
            }
            copy = OnboardingTemplateTask(
                id=uuid7(),
                organization_id=context.organization_id,
                template_version_id=draft.id,
                created_by=context.user_id,
                **data,
            )
            self._tasks.add(copy)
            copies[source.id] = copy
        self._tasks.flush()
        for dep in self._deps.for_version(latest.id):
            self._deps.add(
                OnboardingTemplateTaskDependency(
                    id=uuid7(),
                    organization_id=context.organization_id,
                    template_version_id=draft.id,
                    task_id=copies[dep.task_id].id,
                    prerequisite_task_id=copies[dep.prerequisite_task_id].id,
                    created_by=context.user_id,
                )
            )
        self._deps.flush()
        return draft

    def update_version(
        self,
        context: OrgContext,
        *,
        template_id: uuid.UUID,
        version_id: uuid.UUID,
        changes: dict[str, Any],
        if_match: int | None,
    ) -> OnboardingTemplateVersion:
        self._manage(context)
        version = self._version(template_id, version_id)
        assert_etag(version.row_version, if_match)
        if version.status != "DRAFT":
            raise OnboardingTemplateVersionLocked()
        unknown = set(changes) - VERSION_FIELDS
        if unknown:
            raise ValidationFailed(f"Unknown version fields: {', '.join(sorted(unknown))}.")
        for field, value in changes.items():
            setattr(version, field, value)
        version.updated_by = context.user_id
        return version

    def replace_tasks(
        self,
        context: OrgContext,
        *,
        template_id: uuid.UUID,
        version_id: uuid.UUID,
        tasks: list[dict[str, Any]],
        dependencies: list[dict[str, str]],
    ) -> list[OnboardingTemplateTask]:
        self._manage(context)
        self._templates.lock(template_id)
        version = self._version(template_id, version_id)
        if version.status != "DRAFT":
            raise OnboardingTemplateVersionLocked()
        for dep in self._deps.for_version(version.id):
            self._deps.session.delete(dep)
        self._deps.flush()
        for task in self._tasks.for_version(version.id):
            self._tasks.session.delete(task)
        self._tasks.flush()
        return self._write_tasks(context, version, tasks, dependencies)

    def _write_tasks(
        self,
        context: OrgContext,
        version: OnboardingTemplateVersion,
        tasks: list[dict[str, Any]],
        dependencies: list[dict[str, str]],
    ) -> list[OnboardingTemplateTask]:
        codes = [t["task_code"].upper() for t in tasks]
        if len(set(codes)) != len(codes):
            raise OnboardingTemplateInvalid("Task codes must be unique within a version.")
        rows: dict[str, OnboardingTemplateTask] = {}
        for task in tasks:
            category_id = task.get("required_document_category_id")
            category = (
                self._categories.require(category_id, resource="document category")
                if category_id
                else None
            )
            try:
                normalized = normalize_task(
                    task,
                    category_requires_verification=category.requires_verification
                    if category
                    else None,
                )
            except TaskDefinitionError as error:
                invalid = OnboardingTemplateInvalid(f"Task {task['task_code']} is invalid.")
                for field, message in error.problems:
                    invalid.with_detail(
                        field=f"{task['task_code']}.{field}", code="INVALID", message=message
                    )
                raise invalid from error
            row = OnboardingTemplateTask(
                id=uuid7(),
                organization_id=context.organization_id,
                template_version_id=version.id,
                created_by=context.user_id,
                **{**normalized, "task_code": task["task_code"].upper()},
            )
            self._tasks.add(row)
            rows[row.task_code] = row
        self._tasks.flush()
        edges = []
        for dep in dependencies:
            dependent, prerequisite = (
                dep["task_code"].upper(),
                dep["prerequisite_task_code"].upper(),
            )
            if dependent not in rows or prerequisite not in rows or dependent == prerequisite:
                raise OnboardingTemplateInvalid(
                    "A dependency references an unknown task or itself."
                )
            edges.append((dependent, prerequisite))
        if (cycle := find_cycle(edges)) is not None:
            raise OnboardingTemplateInvalid(
                "The task dependencies contain a cycle: " + " > ".join(cycle)
            )
        for dependent, prerequisite in set(edges):
            self._deps.add(
                OnboardingTemplateTaskDependency(
                    id=uuid7(),
                    organization_id=context.organization_id,
                    template_version_id=version.id,
                    task_id=rows[dependent].id,
                    prerequisite_task_id=rows[prerequisite].id,
                    created_by=context.user_id,
                )
            )
        self._deps.flush()
        return list(rows.values())

    # Publish and archive.

    def publish(  # noqa: PLR0912 - validation list then conflict check, in spec order
        self,
        context: OrgContext,
        *,
        template_id: uuid.UUID,
        version_id: uuid.UUID,
        if_match: int | None,
    ) -> OnboardingTemplateVersion:
        self._manage(context)
        self._templates.lock(
            template_id
        )  # serialises overlap and conflict checks (no exclusion constraints)
        version = self._version(template_id, version_id)
        assert_etag(version.row_version, if_match)
        if version.status != "DRAFT":
            raise InvalidStatusTransition("Only a DRAFT version can be published.")
        problems: list[tuple[str, str]] = []
        if version.effective_from is None:
            problems.append(("effective_from", "Required to publish."))
        if not self._tasks.for_version(version.id):
            problems.append(("tasks", "At least one task is required."))
        for task in self._tasks.for_version(version.id):
            if task.required_document_category_id:
                category = self._categories.get(task.required_document_category_id)
                if category is None or category.status != "ACTIVE":
                    problems.append((task.task_code, "The document category must be ACTIVE."))
        siblings = self._versions.for_templates([template_id])[template_id]
        previous = max(
            (v for v in siblings if v.status == "ACTIVE"),
            key=lambda v: v.version_number,
            default=None,
        )
        if version.version_number >= 2 and not (version.change_note or "").strip():
            problems.append(("change_note", "Required from version 2."))
        if (
            previous is not None
            and previous.effective_from is not None
            and version.effective_from is not None
            and version.effective_from < previous.effective_from + timedelta(days=1)
        ):
            problems.append(("effective_from", "Must be after the previous version's start."))
        if problems:
            error = OnboardingTemplateInvalid("The template version cannot be published.")
            for field, message in problems:
                error.with_detail(field=field, code="INVALID", message=message)
            raise error
        assert version.effective_from is not None  # noqa: S101 - checked in the problems list
        for other, _template in self._versions.active_with_templates():
            if other.template_id == template_id or other.effective_from is None:
                continue
            if (
                all(getattr(other, f) == getattr(version, f) for f in _APPLIES)
                and other.priority == version.priority
                and intervals_overlap(
                    version.effective_from,
                    version.effective_to,
                    other.effective_from,
                    other.effective_to,
                )
            ):
                raise OnboardingTemplateConflict()
        if previous is not None:
            previous.effective_to = version.effective_from
            previous.updated_by = context.user_id
        version.status = "ACTIVE"
        version.published_by = context.user_id
        version.published_at = utc_now()
        version.updated_by = context.user_id
        self._audit.record_change(
            context,
            action=AuditAction.ONBOARDING_TEMPLATE_VERSION_PUBLISHED,
            entity_type="onboarding_template_version",
            entity_id=version.id,
            after={"template_id": str(template_id), "effective_from": version.effective_from},
        )
        return version

    def archive(
        self,
        context: OrgContext,
        *,
        template_id: uuid.UUID,
        version_id: uuid.UUID,
        reason: str,
        if_match: int | None,
    ) -> OnboardingTemplateVersion:
        self._manage(context)
        self._templates.lock(template_id)
        version = self._version(template_id, version_id)
        assert_etag(version.row_version, if_match)
        if version.status != "ACTIVE":
            raise InvalidStatusTransition("Only an ACTIVE version can be archived.")
        version.status = "ARCHIVED"
        version.updated_by = context.user_id
        self._audit.record_change(
            context,
            action=AuditAction.ONBOARDING_TEMPLATE_VERSION_ARCHIVED,
            entity_type="onboarding_template_version",
            entity_id=version.id,
            reason=reason,
        )
        return version

    # Selection (4.4).

    def select(
        self,
        context: OrgContext,
        employee: Employee,
        *,
        template_id: uuid.UUID | None = None,
        basis_date: date | None = None,
    ) -> Selection:
        basis = basis_date or employee.joining_date or self._today(context)
        live = [
            (v, t)
            for v, t in self._versions.active_with_templates()
            if v.effective_from is not None
            and v.effective_from <= basis
            and (v.effective_to is None or basis < v.effective_to)
        ]

        def matches(version: OnboardingTemplateVersion) -> bool:
            return all(
                getattr(version, f"applies_{attr}") in (None, getattr(employee, attr))
                for attr in _EMPLOYEE_ATTRIBUTES
            )

        def order(pair: tuple[OnboardingTemplateVersion, OnboardingTemplate]) -> tuple[int, ...]:
            v = pair[0]
            return (-v.applicability_specificity, v.priority, -v.effective_from.toordinal())  # type: ignore[union-attr]

        candidates = sorted(
            [pair for pair in live if matches(pair[0])],
            key=lambda p: (*order(p), p[1].template_code),
        )
        if template_id is not None:
            chosen = next(((v, t) for v, t in live if t.id == template_id), None)
            if chosen is None:
                raise NoApplicableOnboardingTemplate()
            return Selection(chosen[0], chosen[1], not matches(chosen[0]), "EXPLICIT", candidates)
        if not candidates:
            raise NoApplicableOnboardingTemplate(
                f"No onboarding template applies on {basis.isoformat()}."
            )
        version, template = candidates[0]
        return Selection(version, template, False, "AUTOMATIC", candidates)

    def tasks_of(
        self, version_id: uuid.UUID
    ) -> tuple[list[OnboardingTemplateTask], list[OnboardingTemplateTaskDependency]]:
        return list(self._tasks.for_version(version_id)), list(self._deps.for_version(version_id))
