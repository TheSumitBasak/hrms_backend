"""Repositories for repeated employee records (§12-§13)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import ResourceNotFound
from app.db.base import Base
from app.models.profile import (
    EmployeeEducation,
    EmployeeFamilyMember,
    EmployeeNominee,
    EmployeePreviousEmployment,
    EmployeePreviousEmploymentDocument,
)
from app.repositories.base import OrgScopedRepository, Repository


class _ChildRepository[ModelT: Base](OrgScopedRepository[ModelT]):
    def list_for_employee(self, employee_id: uuid.UUID) -> Sequence[ModelT]:
        statement = (
            self.select()
            .where(self.model.employee_id == employee_id)
            .order_by(self.model.created_at)
        )
        return list(self._session.execute(statement).scalars())

    def require_for_employee(self, employee_id: uuid.UUID, entity_id: uuid.UUID) -> ModelT:
        found = self.get(entity_id)
        if found is None or getattr(found, "employee_id", None) != employee_id:
            raise ResourceNotFound(self.model.__tablename__.rstrip("s"))
        return found


class EducationRepository(_ChildRepository[EmployeeEducation]):
    model = EmployeeEducation


class PreviousEmploymentRepository(_ChildRepository[EmployeePreviousEmployment]):
    model = EmployeePreviousEmployment


class PreviousEmploymentDocumentRepository(Repository):
    model = EmployeePreviousEmploymentDocument

    def __init__(self, session: Session) -> None:
        super().__init__(session)

    def list_for_employment(
        self, employment_id: uuid.UUID
    ) -> Sequence[EmployeePreviousEmploymentDocument]:
        statement = select(EmployeePreviousEmploymentDocument).where(
            EmployeePreviousEmploymentDocument.previous_employment_id == employment_id
        )
        return list(self._session.execute(statement).scalars())

    def replace(self, employment_id: uuid.UUID, document_ids: Sequence[uuid.UUID]) -> None:
        for row in self.list_for_employment(employment_id):
            self._session.delete(row)
        for document_id in document_ids:
            self._session.add(
                EmployeePreviousEmploymentDocument(
                    previous_employment_id=employment_id,
                    document_id=document_id,
                    document_role="PAYSLIP",
                )
            )


class FamilyRepository(_ChildRepository[EmployeeFamilyMember]):
    model = EmployeeFamilyMember


class NominationRepository(OrgScopedRepository[EmployeeNominee]):
    model = EmployeeNominee

    def list_sets(self, employee_id: uuid.UUID) -> Sequence[EmployeeNominee]:
        statement = (
            self.select()
            .where(EmployeeNominee.employee_id == employee_id)
            .order_by(
                EmployeeNominee.nomination_type,
                EmployeeNominee.effective_from.desc(),
                EmployeeNominee.created_at,
            )
        )
        return list(self._session.execute(statement).scalars())

    def current_set(
        self, employee_id: uuid.UUID, nomination_type: str, as_of: date
    ) -> Sequence[EmployeeNominee]:
        statement = self.select().where(
            EmployeeNominee.employee_id == employee_id,
            EmployeeNominee.nomination_type == nomination_type,
            EmployeeNominee.effective_from <= as_of,
            (EmployeeNominee.effective_to.is_(None)) | (EmployeeNominee.effective_to > as_of),
        )
        return list(self._session.execute(statement).scalars())


__all__ = [
    "EducationRepository",
    "FamilyRepository",
    "NominationRepository",
    "PreviousEmploymentDocumentRepository",
    "PreviousEmploymentRepository",
]
