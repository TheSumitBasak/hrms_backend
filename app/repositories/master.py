"""Repositories for organizations' configuration catalogue (§8, §9, §10.2)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any, ClassVar

from sqlalchemy import select

from app.core.errors import InvalidOrganizationReference, MasterInactive
from app.db.base import Base
from app.models.master import (
    MASTER_MODELS,
    BusinessUnit,
    CostCenter,
    Department,
    Designation,
    EmployeeCategory,
    EmployeeCustomStatus,
    EmploymentType,
    Grade,
    LookupValue,
    SubDepartment,
    WorkLocation,
)
from app.repositories.base import OrgScopedRepository
from app.validation.normalizers import escape_like_pattern


class _MasterRepository(OrgScopedRepository[Base]):
    """Shared queries over one master table.

    Each master gets a concrete subclass only to bind :attr:`model`; the queries are written
    once, which is what makes the generic ``/masters/{type}`` endpoint possible without nine
    copies of the same code (DEC-023).
    """

    model: ClassVar[type[Any]] = Base

    def list_masters(self, *, status: str | None = None, query: str | None = None) -> Sequence[Any]:
        statement = self.select()
        if status is not None:
            statement = statement.where(self.model.status == status)
        if query:
            pattern = f"%{escape_like_pattern(query)}%"
            statement = statement.where(
                self.model.name.like(pattern) | self.model.code_normalized.like(pattern.upper())
            )
        statement = statement.order_by(
            self.model.display_order.is_(None),
            self.model.display_order,
            self.model.name,
            self.model.id,
        )
        return list(self._session.execute(statement).scalars())

    def get_by_code(self, code: str) -> Any | None:
        statement = self.select().where(self.model.code_normalized == code.strip().upper())
        return self._session.execute(statement).scalar_one_or_none()

    def name_exists(self, name: str) -> bool:
        statement = (
            select(self.model.id)
            .where(
                self.model.organization_id == self.organization_id,
                self.model.archived_at.is_(None),
                self.model.name == name.strip(),
            )
            .limit(1)
        )
        return self._session.execute(statement).first() is not None


class DepartmentRepository(_MasterRepository):
    model = Department


class SubDepartmentRepository(_MasterRepository):
    model = SubDepartment


class DesignationRepository(_MasterRepository):
    model = Designation


class GradeRepository(_MasterRepository):
    model = Grade


class BusinessUnitRepository(_MasterRepository):
    model = BusinessUnit


class CostCenterRepository(_MasterRepository):
    model = CostCenter


class EmploymentTypeRepository(_MasterRepository):
    model = EmploymentType


class WorkLocationRepository(_MasterRepository):
    model = WorkLocation


class EmployeeCategoryRepository(_MasterRepository):
    model = EmployeeCategory


MASTER_REPOSITORIES: dict[str, type[_MasterRepository]] = {
    "departments": DepartmentRepository,
    "sub-departments": SubDepartmentRepository,
    "designations": DesignationRepository,
    "grades": GradeRepository,
    "business-units": BusinessUnitRepository,
    "cost-centers": CostCenterRepository,
    "employment-types": EmploymentTypeRepository,
    "work-locations": WorkLocationRepository,
    "employee-categories": EmployeeCategoryRepository,
}
"""Path segment to repository, mirroring :data:`app.models.master.MASTER_MODELS`."""


class LookupRepository(OrgScopedRepository[LookupValue]):
    model = LookupValue

    def list_values(
        self, *, list_code: str | None = None, status: str | None = None
    ) -> Sequence[LookupValue]:
        statement = self.select()
        if list_code is not None:
            statement = statement.where(LookupValue.list_code == list_code)
        if status is not None:
            statement = statement.where(LookupValue.status == status)
        statement = statement.order_by(
            LookupValue.list_code, LookupValue.display_order, LookupValue.value_code
        )
        return list(self._session.execute(statement).scalars())

    def get_value(self, list_code: str, value_code: str) -> LookupValue | None:
        statement = self.select().where(
            LookupValue.list_code == list_code,
            LookupValue.value_code == value_code,
        )
        return self._session.execute(statement).scalar_one_or_none()


class CustomStatusRepository(OrgScopedRepository[EmployeeCustomStatus]):
    model = EmployeeCustomStatus

    def list_statuses(self) -> Sequence[EmployeeCustomStatus]:
        statement = self.select().order_by(EmployeeCustomStatus.code)
        return list(self._session.execute(statement).scalars())

    def get_by_code(self, code: str) -> EmployeeCustomStatus | None:
        statement = self.select().where(EmployeeCustomStatus.code == code.strip().upper())
        return self._session.execute(statement).scalar_one_or_none()


class MasterReferenceChecker:
    """Validates employee references against this organization's masters.

    Kept as one small collaborator so employee services do not each re-implement "does this
    id exist in my tenant, and is it active" (§10.7 EMP-07/EMP-08).
    """

    __slots__ = ("_organization_id", "_session")

    def __init__(self, session: Any, organization_id: uuid.UUID) -> None:
        self._session = session
        self._organization_id = organization_id

    def require_active(self, table_name: str, entity_id: uuid.UUID | None) -> Any | None:
        if entity_id is None:
            return None

        repository = master_repository(table_name, self._session, self._organization_id)
        entity: Any = repository.get(entity_id)
        if entity is None:
            raise InvalidOrganizationReference(
                f"That {table_name.rstrip('s')} does not belong to this organization."
            )
        if entity.status != "ACTIVE":
            raise MasterInactive()
        return entity

    def get(self, table_name: str, entity_id: uuid.UUID | None) -> Any | None:
        if entity_id is None:
            return None
        return master_repository(table_name, self._session, self._organization_id).get(entity_id)


def master_repository(
    table_name: str, session: Any, organization_id: uuid.UUID
) -> _MasterRepository:
    """Build the repository for a §24.4.2 master path segment."""
    try:
        repository = MASTER_REPOSITORIES[table_name]
    except KeyError:
        raise KeyError(
            f"unknown master type {table_name!r}; expected one of {sorted(MASTER_MODELS)}"
        ) from None
    return repository(session, organization_id)


__all__ = [
    "MASTER_REPOSITORIES",
    "CustomStatusRepository",
    "LookupRepository",
    "master_repository",
]
