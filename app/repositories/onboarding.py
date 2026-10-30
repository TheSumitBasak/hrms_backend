"""Onboarding repositories (addendum 4). Every query starts from the tenant-filtered select."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import Select, func, or_, select

from app.models.onboarding import (
    OnboardingAcknowledgement,
    OnboardingDataSubmission,
    OnboardingInstance,
    OnboardingInstanceEvent,
    OnboardingTask,
    OnboardingTaskDependency,
    OnboardingTaskDocument,
    OnboardingTaskEvent,
    OnboardingTemplate,
    OnboardingTemplateTask,
    OnboardingTemplateTaskDependency,
    OnboardingTemplateVersion,
)
from app.repositories.base import OrgScopedRepository
from app.services.onboarding_rules import OPEN_STATUSES


class TemplateRepository(OrgScopedRepository[OnboardingTemplate]):
    model = OnboardingTemplate

    def list_templates(self, *, status: str | None = None) -> Sequence[OnboardingTemplate]:
        statement = self.select().order_by(OnboardingTemplate.template_code)
        if status:
            statement = statement.where(OnboardingTemplate.status == status)
        return list(self._session.execute(statement).scalars())

    def code_exists(self, code: str) -> bool:
        statement = select(OnboardingTemplate.id).where(
            OnboardingTemplate.organization_id == self._organization_id,
            OnboardingTemplate.template_code_normalized == code.upper(),
        )
        return self._session.execute(statement.limit(1)).first() is not None


class TemplateVersionRepository(OrgScopedRepository[OnboardingTemplateVersion]):
    model = OnboardingTemplateVersion

    def for_templates(
        self, template_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, list[OnboardingTemplateVersion]]:
        grouped: dict[uuid.UUID, list[OnboardingTemplateVersion]] = {t: [] for t in template_ids}
        if template_ids:
            statement = (
                self.select()
                .where(OnboardingTemplateVersion.template_id.in_(template_ids))
                .order_by(OnboardingTemplateVersion.version_number)
            )
            for row in self._session.execute(statement).scalars():
                grouped[row.template_id].append(row)
        return grouped

    def active_with_templates(self) -> list[tuple[OnboardingTemplateVersion, OnboardingTemplate]]:
        """ACTIVE versions of ACTIVE templates (the 4.4 candidate set before date filtering)."""
        statement = (
            select(OnboardingTemplateVersion, OnboardingTemplate)
            .join(
                OnboardingTemplate,
                (OnboardingTemplate.id == OnboardingTemplateVersion.template_id)
                & (OnboardingTemplate.organization_id == OnboardingTemplateVersion.organization_id),
            )
            .where(
                OnboardingTemplateVersion.organization_id == self._organization_id,
                OnboardingTemplateVersion.status == "ACTIVE",
                OnboardingTemplate.status == "ACTIVE",
            )
        )
        return [(v, t) for v, t in self._session.execute(statement).all()]


class TemplateTaskRepository(OrgScopedRepository[OnboardingTemplateTask]):
    model = OnboardingTemplateTask

    def for_version(self, version_id: uuid.UUID) -> Sequence[OnboardingTemplateTask]:
        statement = (
            self.select()
            .where(OnboardingTemplateTask.template_version_id == version_id)
            .order_by(OnboardingTemplateTask.display_order, OnboardingTemplateTask.task_code)
        )
        return list(self._session.execute(statement).scalars())


class TemplateDependencyRepository(OrgScopedRepository[OnboardingTemplateTaskDependency]):
    model = OnboardingTemplateTaskDependency

    def for_version(self, version_id: uuid.UUID) -> Sequence[OnboardingTemplateTaskDependency]:
        statement = self.select().where(
            OnboardingTemplateTaskDependency.template_version_id == version_id
        )
        return list(self._session.execute(statement).scalars())


class InstanceRepository(OrgScopedRepository[OnboardingInstance]):
    model = OnboardingInstance

    def open_for_employee(self, employee_id: uuid.UUID) -> OnboardingInstance | None:
        """The non-cancelled instance of a record (ONB-02), if any."""
        statement = self.select().where(
            OnboardingInstance.employee_id == employee_id, OnboardingInstance.status != "CANCELLED"
        )
        return self._session.execute(statement).scalar_one_or_none()

    def for_employee(
        self, employee_id: uuid.UUID, *, include_cancelled: bool
    ) -> Sequence[OnboardingInstance]:
        statement = (
            self.select()
            .where(OnboardingInstance.employee_id == employee_id)
            .order_by(OnboardingInstance.created_at.desc())
        )
        if not include_cancelled:
            statement = statement.where(OnboardingInstance.status != "CANCELLED")
        return list(self._session.execute(statement).scalars())

    def list_query(
        self, *, statuses: Sequence[str] | None, employee_ids: Sequence[uuid.UUID] | None
    ) -> Select[tuple[OnboardingInstance]]:
        statement = self.select()
        if statuses:
            statement = statement.where(OnboardingInstance.status.in_(statuses))
        if employee_ids is not None:
            statement = statement.where(OnboardingInstance.employee_id.in_(employee_ids))
        return statement


class TaskRepository(OrgScopedRepository[OnboardingTask]):
    model = OnboardingTask

    def for_instance(self, instance_id: uuid.UUID) -> Sequence[OnboardingTask]:
        statement = (
            self.select()
            .where(OnboardingTask.instance_id == instance_id)
            .order_by(OnboardingTask.display_order, OnboardingTask.task_code)
        )
        return list(self._session.execute(statement).scalars())

    def locked_for_instance(self, instance_id: uuid.UUID) -> Sequence[OnboardingTask]:
        """Lock every task of an instance in ascending id order (7.3)."""
        statement = (
            self.select()
            .where(OnboardingTask.instance_id == instance_id)
            .order_by(OnboardingTask.id)
            .with_for_update()
        )
        rows = list(self._session.execute(statement).scalars())
        for row in rows:
            self._session.refresh(row)
        return rows

    def queue(
        self,
        *,
        user_id: uuid.UUID,
        department_id: uuid.UUID | None,
        statuses: Sequence[str] | None,
        review: bool,
    ) -> Sequence[OnboardingTask]:
        column_user = OnboardingTask.reviewer_user_id if review else OnboardingTask.assignee_user_id
        column_dept = (
            OnboardingTask.reviewer_department_id
            if review
            else OnboardingTask.assignee_department_id
        )
        conditions = [column_user == user_id]
        if department_id is not None:
            conditions.append(column_dept == department_id)
        statement = (
            self.select()
            .where(or_(*conditions), OnboardingTask.status.in_(statuses or OPEN_STATUSES))
            .order_by(OnboardingTask.due_date, OnboardingTask.id)
            .limit(200)
        )
        return list(self._session.execute(statement).scalars())

    def open_for_employee_with_joining_basis(
        self, employee_id: uuid.UUID
    ) -> Sequence[OnboardingTask]:
        statement = self.select().where(
            OnboardingTask.employee_id == employee_id,
            OnboardingTask.due_basis == "JOINING_DATE",
            OnboardingTask.due_date_source == "COMPUTED",
            OnboardingTask.status.in_(OPEN_STATUSES),
        )
        return list(self._session.execute(statement).scalars())


class TaskDependencyRepository(OrgScopedRepository[OnboardingTaskDependency]):
    model = OnboardingTaskDependency

    def for_instance(self, instance_id: uuid.UUID) -> Sequence[OnboardingTaskDependency]:
        statement = self.select().where(OnboardingTaskDependency.instance_id == instance_id)
        return list(self._session.execute(statement).scalars())


class InstanceEventRepository(OrgScopedRepository[OnboardingInstanceEvent]):
    model = OnboardingInstanceEvent

    def for_instance(self, instance_id: uuid.UUID) -> Sequence[OnboardingInstanceEvent]:
        statement = (
            self.select()
            .where(OnboardingInstanceEvent.instance_id == instance_id)
            .order_by(OnboardingInstanceEvent.occurred_at, OnboardingInstanceEvent.id)
        )
        return list(self._session.execute(statement).scalars())


class TaskEventRepository(OrgScopedRepository[OnboardingTaskEvent]):
    model = OnboardingTaskEvent

    def for_task(self, task_id: uuid.UUID) -> Sequence[OnboardingTaskEvent]:
        statement = (
            self.select()
            .where(OnboardingTaskEvent.task_id == task_id)
            .order_by(OnboardingTaskEvent.occurred_at, OnboardingTaskEvent.id)
        )
        return list(self._session.execute(statement).scalars())

    def last_reopened_at(self, task_id: uuid.UUID) -> datetime | None:
        statement = select(func.max(OnboardingTaskEvent.occurred_at)).where(
            OnboardingTaskEvent.organization_id == self._organization_id,
            OnboardingTaskEvent.task_id == task_id,
            OnboardingTaskEvent.event_type == "REOPENED",
        )
        return self._session.execute(statement).scalar_one_or_none()


class TaskDocumentRepository(OrgScopedRepository[OnboardingTaskDocument]):
    model = OnboardingTaskDocument

    def for_task(
        self, task_id: uuid.UUID, *, active_only: bool = False
    ) -> Sequence[OnboardingTaskDocument]:
        statement = self.select().where(OnboardingTaskDocument.task_id == task_id)
        if active_only:
            statement = statement.where(OnboardingTaskDocument.link_status == "ACTIVE")
        return list(self._session.execute(statement).scalars())

    def find(self, task_id: uuid.UUID, document_id: uuid.UUID) -> OnboardingTaskDocument | None:
        statement = self.select().where(
            OnboardingTaskDocument.task_id == task_id,
            OnboardingTaskDocument.document_id == document_id,
        )
        return self._session.execute(statement).scalar_one_or_none()

    def active_for_document(self, document_id: uuid.UUID) -> Sequence[OnboardingTaskDocument]:
        statement = self.select().where(
            OnboardingTaskDocument.document_id == document_id,
            OnboardingTaskDocument.link_status == "ACTIVE",
        )
        return list(self._session.execute(statement).scalars())


class AcknowledgementRepository(OrgScopedRepository[OnboardingAcknowledgement]):
    model = OnboardingAcknowledgement

    def latest_at(self, task_id: uuid.UUID) -> datetime | None:
        statement = select(func.max(OnboardingAcknowledgement.acknowledged_at)).where(
            OnboardingAcknowledgement.organization_id == self._organization_id,
            OnboardingAcknowledgement.task_id == task_id,
        )
        return self._session.execute(statement).scalar_one_or_none()


class SubmissionRepository(OrgScopedRepository[OnboardingDataSubmission]):
    model = OnboardingDataSubmission

    def for_task(self, task_id: uuid.UUID) -> Sequence[OnboardingDataSubmission]:
        statement = (
            self.select()
            .where(OnboardingDataSubmission.task_id == task_id)
            .order_by(OnboardingDataSubmission.submission_no)
        )
        return list(self._session.execute(statement).scalars())

    def next_number(self, task_id: uuid.UUID) -> int:
        statement = select(
            func.coalesce(func.max(OnboardingDataSubmission.submission_no), 0)
        ).where(
            OnboardingDataSubmission.organization_id == self._organization_id,
            OnboardingDataSubmission.task_id == task_id,
        )
        return int(self._session.execute(statement).scalar_one()) + 1
