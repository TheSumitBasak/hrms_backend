"""Employee reads needed by the catalogue services (§8-§10).

The full employee repository lands with §24.4.3; this file starts with the reference checks a
branch or master needs, so those services never reach for a raw ``Session``.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date
from typing import Any

from sqlalchemy import func, or_, select

from app.models.employee import (
    Employee,
    EmployeeAddress,
    EmployeeJobHistory,
    EmployeeStatus,
    EmployeeStatusHistory,
)
from app.models.master import Department, Designation
from app.repositories.base import OrgScopedRepository
from app.validation.normalizers import escape_like_pattern, normalize_search_name

ACTIVE_LIKE_STATUSES: frozenset[str] = frozenset(
    {
        EmployeeStatus.ACTIVE.value,
        EmployeeStatus.ON_NOTICE.value,
    }
)


class EmployeeRepository(OrgScopedRepository[Employee]):
    model = Employee

    def count_assigned_to_branch(self, branch_id: uuid.UUID) -> int:
        statement = (
            select(func.count())
            .select_from(Employee)
            .where(
                Employee.organization_id == self.organization_id,
                Employee.branch_id == branch_id,
                Employee.archived_at.is_(None),
                Employee.status.in_(ACTIVE_LIKE_STATUSES),
            )
        )
        return int(self._session.execute(statement).scalar() or 0)

    def get(self, employee_id: uuid.UUID, *, include_archived: bool = False) -> Employee | None:
        return super().get(employee_id, include_archived=include_archived)

    def get_any_org(self, employee_id: uuid.UUID) -> Employee | None:
        statement = select(Employee).where(Employee.id == employee_id).limit(1)
        return self._session.execute(statement).scalar_one_or_none()

    def get_for_update(self, employee_id: uuid.UUID) -> Employee | None:
        statement = (
            self.select(include_archived=True).where(Employee.id == employee_id).with_for_update()
        )
        return self._session.execute(statement).scalar_one_or_none()

    def email_exists(
        self, email: str, *, organization_ids: Sequence[uuid.UUID] | None = None
    ) -> bool:
        """Whether a live employee already uses this official email (§10.5)."""
        statement = (
            select(Employee.id)
            .where(
                Employee.official_email == email.lower(),
                Employee.archived_at.is_(None),
                ~Employee.status.in_(
                    [
                        EmployeeStatus.RESIGNED.value,
                        EmployeeStatus.TERMINATED.value,
                        EmployeeStatus.RETIRED.value,
                    ]
                ),
            )
            .limit(1)
        )
        if organization_ids is not None:
            statement = statement.where(Employee.organization_id.in_(organization_ids))
        return self._session.execute(statement).first() is not None

    def get_by_code(self, employee_code: str) -> Employee | None:
        statement = self.select().where(Employee.employee_code_normalized == employee_code.upper())
        return self._session.execute(statement).scalar_one_or_none()

    def code_exists(self, employee_code: str) -> bool:
        statement = (
            select(Employee.id)
            .where(
                Employee.organization_id == self.organization_id,
                Employee.employee_code_normalized == employee_code.upper(),
            )
            .limit(1)
        )
        return self._session.execute(statement).first() is not None

    def search(
        self,
        *,
        query: str | None = None,
        status: Sequence[str] | None = None,
        department_id: uuid.UUID | None = None,
        designation_id: uuid.UUID | None = None,
        branch_id: uuid.UUID | None = None,
        limit: int = 50,
    ) -> Sequence[Employee]:
        statement = self.select()
        if query:
            normalized = normalize_search_name(query)
            statement = statement.where(
                or_(
                    Employee.full_name_normalized.like(f"%{normalized}%"),
                    Employee.employee_code_normalized.like(f"{query.upper()}%"),
                    Employee.official_email == query.lower(),
                )
            )
        if status:
            statement = statement.where(Employee.status.in_(status))
        if department_id is not None:
            statement = statement.where(Employee.department_id == department_id)
        if designation_id is not None:
            statement = statement.where(Employee.designation_id == designation_id)
        if branch_id is not None:
            statement = statement.where(Employee.branch_id == branch_id)
        statement = statement.order_by(Employee.full_name_normalized, Employee.id).limit(limit)
        return list(self._session.execute(statement).scalars())

    def search_page(  # noqa: PLR0912, PLR0915 - one predicate per §26.2 filter
        self,
        *,
        query: str | None = None,
        employee_code: str | None = None,
        official_email: str | None = None,
        status: Sequence[str] | None = None,
        department_ids: Sequence[uuid.UUID] | None = None,
        designation_ids: Sequence[uuid.UUID] | None = None,
        branch_ids: Sequence[uuid.UUID] | None = None,
        employment_type_ids: Sequence[uuid.UUID] | None = None,
        grade_ids: Sequence[uuid.UUID] | None = None,
        business_unit_ids: Sequence[uuid.UUID] | None = None,
        cost_center_ids: Sequence[uuid.UUID] | None = None,
        work_location_ids: Sequence[uuid.UUID] | None = None,
        employee_category_ids: Sequence[uuid.UUID] | None = None,
        work_modes: Sequence[str] | None = None,
        joining_from: date | None = None,
        joining_to: date | None = None,
        reporting_manager_employee_id: uuid.UUID | None = None,
        employee_ids: Sequence[uuid.UUID] | None = None,
        include_archived: bool = False,
        sort_field: str = "display_name",
        direction: str = "asc",
        after_value: Any | None = None,
        after_id: uuid.UUID | None = None,
        limit: int = 25,
        with_total: bool = False,
        total_limit: int = 10000,
    ) -> tuple[list[tuple[Employee, str | None, str | None]], bool, int | None]:
        conditions: list[Any] = []
        statement = (
            select(Employee, Department.name, Designation.name)
            .outerjoin(
                Department,
                (Department.id == Employee.department_id)
                & (Department.organization_id == self.organization_id),
            )
            .outerjoin(
                Designation,
                (Designation.id == Employee.designation_id)
                & (Designation.organization_id == self.organization_id),
            )
            .where(Employee.organization_id == self.organization_id)
        )
        if not include_archived:
            conditions.append(Employee.archived_at.is_(None))
        if query:
            normalized = normalize_search_name(query)
            escaped = escape_like_pattern(query)
            conditions.append(
                or_(
                    Employee.full_name_normalized.like(f"%{normalized}%"),
                    Employee.employee_code_normalized.like(f"{escaped.upper()}%"),
                    Employee.official_email.like(f"{escaped.lower()}%"),
                )
            )
        if employee_code:
            conditions.append(Employee.employee_code_normalized == employee_code.upper())
        if official_email:
            conditions.append(Employee.official_email == official_email.lower())
        if status:
            conditions.append(Employee.status.in_(status))
        if department_ids:
            conditions.append(Employee.department_id.in_(department_ids))
        if designation_ids:
            conditions.append(Employee.designation_id.in_(designation_ids))
        if branch_ids:
            conditions.append(Employee.branch_id.in_(branch_ids))
        if employment_type_ids:
            conditions.append(Employee.employment_type_id.in_(employment_type_ids))
        if grade_ids:
            conditions.append(Employee.grade_id.in_(grade_ids))
        if business_unit_ids:
            conditions.append(Employee.business_unit_id.in_(business_unit_ids))
        if cost_center_ids:
            conditions.append(Employee.cost_center_id.in_(cost_center_ids))
        if work_location_ids:
            conditions.append(Employee.work_location_id.in_(work_location_ids))
        if employee_category_ids:
            conditions.append(Employee.employee_category_id.in_(employee_category_ids))
        if work_modes:
            conditions.append(Employee.work_mode.in_(work_modes))
        if joining_from:
            conditions.append(Employee.joining_date >= joining_from)
        if joining_to:
            conditions.append(Employee.joining_date <= joining_to)
        if employee_ids is not None:
            conditions.append(Employee.id.in_(employee_ids))
        if reporting_manager_employee_id:
            conditions.append(
                Employee.reporting_manager_employee_id == reporting_manager_employee_id
            )
        statement = statement.where(*conditions)
        filter_conditions = list(conditions)

        columns = {
            "employee_code": Employee.employee_code_normalized,
            "display_name": Employee.display_name,
            "joining_date": Employee.joining_date,
            "department_name": Department.name,
            "designation_name": Designation.name,
            "status": Employee.status,
            "created_at": Employee.created_at,
        }
        if sort_field not in columns:
            sort_field = "display_name"
        sort_column = columns[sort_field]
        if after_value is not None and after_id is not None:
            comparison = (
                sort_column > after_value if direction == "asc" else sort_column < after_value
            )
            conditions.append(
                or_(
                    comparison,
                    (sort_column == after_value) & (Employee.id > after_id),
                )
            )
            statement = statement.where(conditions[-1])
        ordered = sort_column.asc() if direction == "asc" else sort_column.desc()
        statement = statement.order_by(ordered, Employee.id.asc()).limit(limit + 1)
        rows = list(self._session.execute(statement).all())
        has_more = len(rows) > limit
        rows = rows[:limit]
        total = None
        if with_total:
            count_statement = (
                select(func.count())
                .select_from(Employee)
                .where(
                    Employee.organization_id == self.organization_id,
                    *filter_conditions,
                )
            )
            total = int(self._session.execute(count_statement).scalar() or 0)
            if total > total_limit:
                total = None
        return [(row[0], row[1], row[2]) for row in rows], has_more, total

    def descendant_ids(self, manager_id: uuid.UUID, *, max_depth: int = 20) -> list[uuid.UUID]:
        frontier = {manager_id}
        found: set[uuid.UUID] = set()
        for _ in range(max_depth):
            if not frontier:
                break
            rows = select(Employee.id).where(
                Employee.organization_id == self.organization_id,
                Employee.reporting_manager_employee_id.in_(frontier),
            )
            frontier = set(self._session.execute(rows).scalars()) - found
            found |= frontier
        return list(found)

    def list_for_reference_check(self, employee_ids: Sequence[uuid.UUID]) -> Sequence[Employee]:
        if not employee_ids:
            return []
        statement = self.select().where(Employee.id.in_(employee_ids))
        return list(self._session.execute(statement).scalars())


class EmployeeAddressRepository(OrgScopedRepository[EmployeeAddress]):
    model = EmployeeAddress

    def list_for_employee(self, employee_id: uuid.UUID) -> Sequence[EmployeeAddress]:
        statement = self.select().where(EmployeeAddress.employee_id == employee_id)
        return list(self._session.execute(statement).scalars())

    def get_by_type(self, employee_id: uuid.UUID, address_type: str) -> EmployeeAddress | None:
        statement = self.select().where(
            EmployeeAddress.employee_id == employee_id,
            EmployeeAddress.address_type == address_type,
        )
        return self._session.execute(statement).scalar_one_or_none()

    def delete_for_employee(self, employee_id: uuid.UUID, address_type: str) -> None:
        found = self.get_by_type(employee_id, address_type)
        if found is not None:
            self._session.delete(found)


class EmployeeJobHistoryRepository(OrgScopedRepository[EmployeeJobHistory]):
    model = EmployeeJobHistory

    def list_for_employee(self, employee_id: uuid.UUID) -> Sequence[EmployeeJobHistory]:
        statement = (
            self.select(include_archived=True)
            .where(EmployeeJobHistory.employee_id == employee_id)
            .order_by(
                EmployeeJobHistory.effective_date.desc(),
                EmployeeJobHistory.sequence_no.desc(),
            )
        )
        return list(self._session.execute(statement).scalars())

    def latest(self, employee_id: uuid.UUID) -> EmployeeJobHistory | None:
        statement = (
            self.select(include_archived=True)
            .where(EmployeeJobHistory.employee_id == employee_id)
            .order_by(
                EmployeeJobHistory.effective_date.desc(),
                EmployeeJobHistory.sequence_no.desc(),
            )
            .limit(1)
        )
        return self._session.execute(statement).scalar_one_or_none()


class EmployeeStatusHistoryRepository(OrgScopedRepository[EmployeeStatusHistory]):
    model = EmployeeStatusHistory

    def list_for_employee(self, employee_id: uuid.UUID) -> Sequence[EmployeeStatusHistory]:
        statement = (
            self.select(include_archived=True)
            .where(EmployeeStatusHistory.employee_id == employee_id)
            .order_by(
                EmployeeStatusHistory.status_effective_date.desc(),
                EmployeeStatusHistory.created_at.desc(),
            )
        )
        return list(self._session.execute(statement).scalars())

    def latest(self, employee_id: uuid.UUID) -> EmployeeStatusHistory | None:
        statement = (
            self.select(include_archived=True)
            .where(EmployeeStatusHistory.employee_id == employee_id)
            .order_by(
                EmployeeStatusHistory.status_effective_date.desc(),
                EmployeeStatusHistory.created_at.desc(),
            )
            .limit(1)
        )
        return self._session.execute(statement).scalar_one_or_none()


__all__ = [
    "ACTIVE_LIKE_STATUSES",
    "EmployeeAddressRepository",
    "EmployeeJobHistoryRepository",
    "EmployeeRepository",
    "EmployeeStatusHistoryRepository",
]
