"""Repositories for salary components, templates and assignments (§16-§18)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date
from typing import Any

from sqlalchemy import func, select

from app.core.errors import ResourceNotFound
from app.models.salary import (
    CONFIRMED_STATUSES,
    EmployeeSalaryAssignment,
    EmployeeSalaryComponent,
    SalaryComponent,
    SalaryRevisionHistory,
    SalaryTemplate,
    SalaryTemplateComponent,
    SalaryTemplateVersion,
)
from app.repositories.base import OrgScopedRepository, Repository


class SalaryComponentRepository(OrgScopedRepository[SalaryComponent]):
    model = SalaryComponent

    def list_components(self, *, include_inactive: bool = False) -> Sequence[SalaryComponent]:
        statement = self.select()
        if not include_inactive:
            statement = statement.where(SalaryComponent.status == "ACTIVE")
        statement = statement.order_by(
            SalaryComponent.display_order, SalaryComponent.component_name
        )
        return list(self._session.execute(statement).scalars())

    def get_by_code(self, code: str) -> SalaryComponent | None:
        statement = self.select().where(
            SalaryComponent.component_code_normalized == code.strip().upper()
        )
        return self._session.execute(statement).scalar_one_or_none()


class SalaryTemplateRepository(OrgScopedRepository[SalaryTemplate]):
    model = SalaryTemplate

    def list_templates(self, *, include_archived: bool = False) -> Sequence[SalaryTemplate]:
        statement = self.select(include_archived=True)
        if not include_archived:
            statement = statement.where(SalaryTemplate.is_archived.is_(False))
        statement = statement.order_by(SalaryTemplate.template_name)
        return list(self._session.execute(statement).scalars())

    def get_by_code(self, code: str) -> SalaryTemplate | None:
        statement = self.select(include_archived=True).where(
            SalaryTemplate.template_code_normalized == code.strip().upper()
        )
        return self._session.execute(statement).scalar_one_or_none()


class SalaryTemplateVersionRepository(OrgScopedRepository[SalaryTemplateVersion]):
    model = SalaryTemplateVersion

    def list_for_template(self, template_id: uuid.UUID) -> Sequence[SalaryTemplateVersion]:
        statement = (
            self.select(include_archived=True)
            .where(SalaryTemplateVersion.template_id == template_id)
            .order_by(SalaryTemplateVersion.version_number)
        )
        return list(self._session.execute(statement).scalars())

    def latest(self, template_id: uuid.UUID) -> SalaryTemplateVersion | None:
        statement = (
            self.select(include_archived=True)
            .where(SalaryTemplateVersion.template_id == template_id)
            .order_by(SalaryTemplateVersion.version_number.desc())
            .limit(1)
        )
        return self._session.execute(statement).scalars().first()

    def open_draft(self, template_id: uuid.UUID) -> SalaryTemplateVersion | None:
        statement = self.select().where(
            SalaryTemplateVersion.template_id == template_id,
            SalaryTemplateVersion.status == "DRAFT",
        )
        return self._session.execute(statement).scalars().first()

    def effective_on(self, template_id: uuid.UUID, moment: date) -> SalaryTemplateVersion | None:
        statement = self.select().where(
            SalaryTemplateVersion.template_id == template_id,
            SalaryTemplateVersion.status == "ACTIVE",
            SalaryTemplateVersion.effective_from <= moment,
            (SalaryTemplateVersion.effective_to.is_(None))
            | (SalaryTemplateVersion.effective_to > moment),
        )
        return self._session.execute(statement).scalars().first()

    def overlapping(
        self, template_id: uuid.UUID, start: date, end: date | None, exclude_id: uuid.UUID
    ) -> Sequence[SalaryTemplateVersion]:
        statement = self.select().where(
            SalaryTemplateVersion.template_id == template_id,
            SalaryTemplateVersion.id != exclude_id,
            SalaryTemplateVersion.status.in_(["ACTIVE"]),
        )
        rows = list(self._session.execute(statement).scalars())
        return [
            row
            for row in rows
            if row.effective_from is not None
            and row.effective_from < (end or date.max)
            and (row.effective_to is None or start < row.effective_to)
        ]


class SalaryTemplateComponentRepository(OrgScopedRepository[SalaryTemplateComponent]):
    model = SalaryTemplateComponent

    def exists_component(self, component_id: uuid.UUID) -> bool:
        statement = (
            select(SalaryTemplateComponent.id)
            .where(
                SalaryTemplateComponent.organization_id == self.organization_id,
                SalaryTemplateComponent.component_id == component_id,
            )
            .limit(1)
        )
        return self._session.execute(statement).first() is not None

    def count_versions_using(self, component_id: uuid.UUID) -> int:
        statement = select(
            func.count(func.distinct(SalaryTemplateComponent.template_version_id))
        ).where(
            SalaryTemplateComponent.organization_id == self.organization_id,
            SalaryTemplateComponent.component_id == component_id,
        )
        return int(self._session.execute(statement).scalar_one())

    def list_for_version(self, version_id: uuid.UUID) -> Sequence[SalaryTemplateComponent]:
        statement = (
            self.select(include_archived=True)
            .where(SalaryTemplateComponent.template_version_id == version_id)
            .order_by(SalaryTemplateComponent.display_order)
        )
        return list(self._session.execute(statement).scalars())

    def replace(
        self, version: SalaryTemplateVersion, rows: Sequence[SalaryTemplateComponent]
    ) -> None:
        for existing in self.list_for_version(version.id):
            self._session.delete(existing)
        # Flush the deletes first: SQLAlchemy runs inserts before deletes in one flush, which
        # would trip the (version, component) unique key when a row is replaced by itself.
        self._session.flush()
        for row in rows:
            self.add(row)


class SalaryAssignmentRepository(OrgScopedRepository[EmployeeSalaryAssignment]):
    model = EmployeeSalaryAssignment

    def list_for_employee(
        self,
        employee_id: uuid.UUID,
        *,
        include_cancelled: bool = False,
        confirmed_only: bool = False,
    ) -> Sequence[EmployeeSalaryAssignment]:
        statement = self.select(include_archived=True).where(
            EmployeeSalaryAssignment.employee_id == employee_id
        )
        if confirmed_only:
            statement = statement.where(
                EmployeeSalaryAssignment.assignment_status.in_(
                    [status.value for status in CONFIRMED_STATUSES]
                )
            )
        elif not include_cancelled:
            statement = statement.where(EmployeeSalaryAssignment.assignment_status != "CANCELLED")
        statement = statement.order_by(
            EmployeeSalaryAssignment.effective_from, EmployeeSalaryAssignment.created_at
        )
        return list(self._session.execute(statement).scalars())

    def confirmed_for_employee(self, employee_id: uuid.UUID) -> Sequence[EmployeeSalaryAssignment]:
        return self.list_for_employee(employee_id, confirmed_only=True)

    def as_of(self, employee_id: uuid.UUID, moment: date) -> EmployeeSalaryAssignment | None:
        statement = self.select(include_archived=True).where(
            EmployeeSalaryAssignment.employee_id == employee_id,
            EmployeeSalaryAssignment.assignment_status.in_(
                [status.value for status in CONFIRMED_STATUSES]
            ),
            EmployeeSalaryAssignment.effective_from <= moment,
            (EmployeeSalaryAssignment.effective_to.is_(None))
            | (EmployeeSalaryAssignment.effective_to > moment),
        )
        return self._session.execute(statement).scalars().first()

    def require_for_employee(
        self, employee_id: uuid.UUID, assignment_id: uuid.UUID
    ) -> EmployeeSalaryAssignment:
        row = self.get(assignment_id, include_archived=True)
        if row is None or row.employee_id != employee_id:
            raise ResourceNotFound("salary assignment")
        return row


class AssignmentComponentRepository(Repository):
    model = EmployeeSalaryComponent

    def __init__(self, session: Any) -> None:
        super().__init__(session)

    def exists_component(self, component_id: uuid.UUID) -> bool:
        statement = (
            select(EmployeeSalaryComponent.id)
            .where(EmployeeSalaryComponent.component_id == component_id)
            .limit(1)
        )
        return self._session.execute(statement).first() is not None

    def count_assignments_using(self, component_id: uuid.UUID) -> int:
        statement = select(
            func.count(func.distinct(EmployeeSalaryComponent.salary_assignment_id))
        ).where(EmployeeSalaryComponent.component_id == component_id)
        return int(self._session.execute(statement).scalar_one())

    def list_for_assignment(self, assignment_id: uuid.UUID) -> Sequence[EmployeeSalaryComponent]:
        statement = (
            select(EmployeeSalaryComponent)
            .where(EmployeeSalaryComponent.salary_assignment_id == assignment_id)
            .where(EmployeeSalaryComponent.archived_at.is_(None))
            .order_by(EmployeeSalaryComponent.display_order_snapshot)
        )
        return list(self._session.execute(statement).scalars())

    def replace(self, assignment_id: uuid.UUID, rows: Sequence[EmployeeSalaryComponent]) -> None:
        for existing in self.list_for_assignment(assignment_id):
            self._session.delete(existing)
        self._session.flush()  # deletes before inserts, as in the template repository
        for row in rows:
            self._session.add(row)


class SalaryRevisionHistoryRepository(OrgScopedRepository[SalaryRevisionHistory]):
    model = SalaryRevisionHistory

    def list_for_employee(self, employee_id: uuid.UUID) -> Sequence[SalaryRevisionHistory]:
        statement = (
            self.select(include_archived=True)
            .where(SalaryRevisionHistory.employee_id == employee_id)
            .order_by(SalaryRevisionHistory.occurred_at.desc())
        )
        return list(self._session.execute(statement).scalars())


__all__ = [
    "AssignmentComponentRepository",
    "SalaryAssignmentRepository",
    "SalaryComponentRepository",
    "SalaryRevisionHistoryRepository",
    "SalaryTemplateComponentRepository",
    "SalaryTemplateRepository",
    "SalaryTemplateVersionRepository",
]
