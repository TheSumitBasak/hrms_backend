"""Reads and writes for encrypted employee identifiers (§11)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from app.models.identifier import EmployeeIdentifier
from app.repositories.base import OrgScopedRepository


class EmployeeIdentifierRepository(OrgScopedRepository[EmployeeIdentifier]):
    model = EmployeeIdentifier

    def list_for_employee(
        self, employee_id: uuid.UUID, *, current_only: bool = False
    ) -> Sequence[EmployeeIdentifier]:
        statement = self.select().where(EmployeeIdentifier.employee_id == employee_id)
        if current_only:
            statement = statement.where(EmployeeIdentifier.is_current.is_(True))
        statement = statement.order_by(
            EmployeeIdentifier.identifier_type, EmployeeIdentifier.created_at.desc()
        )
        return list(self._session.execute(statement).scalars())

    def current_for_type(
        self, employee_id: uuid.UUID, identifier_type: str
    ) -> Sequence[EmployeeIdentifier]:
        statement = self.select().where(
            EmployeeIdentifier.employee_id == employee_id,
            EmployeeIdentifier.identifier_type == identifier_type,
            EmployeeIdentifier.is_current.is_(True),
        )
        return list(self._session.execute(statement).scalars())

    def find_duplicates(
        self,
        *,
        identifier_type: str,
        blind_index: bytes,
        organization_ids: Sequence[uuid.UUID],
        exclude_employee_id: uuid.UUID | None = None,
    ) -> Sequence[EmployeeIdentifier]:
        if not organization_ids:
            return []
        statement = self.select().where(
            EmployeeIdentifier.identifier_type == identifier_type,
            EmployeeIdentifier.value_blind_index == blind_index,
            EmployeeIdentifier.organization_id.in_(organization_ids),
            EmployeeIdentifier.is_current.is_(True),
        )
        if exclude_employee_id is not None:
            statement = statement.where(EmployeeIdentifier.employee_id != exclude_employee_id)
        return list(self._session.execute(statement).scalars())


__all__ = ["EmployeeIdentifierRepository"]
