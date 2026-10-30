"""Bank account reads and writes (§14)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from app.models.profile import EmployeeBankAccount
from app.repositories.base import OrgScopedRepository


class EmployeeBankAccountRepository(OrgScopedRepository[EmployeeBankAccount]):
    model = EmployeeBankAccount

    def list_for_employee(self, employee_id: uuid.UUID) -> Sequence[EmployeeBankAccount]:
        statement = (
            self.select()
            .where(EmployeeBankAccount.employee_id == employee_id)
            .order_by(EmployeeBankAccount.effective_from.desc())
        )
        return list(self._session.execute(statement).scalars())

    def primary_for_employee(self, employee_id: uuid.UUID) -> EmployeeBankAccount | None:
        statement = self.select().where(
            EmployeeBankAccount.employee_id == employee_id,
            EmployeeBankAccount.is_primary.is_(True),
        )
        return self._session.execute(statement).scalars().first()

    def find_duplicate(
        self, *, blind_index: bytes, organization_ids: Sequence[uuid.UUID]
    ) -> EmployeeBankAccount | None:
        if not organization_ids:
            return None
        statement = self.select().where(
            EmployeeBankAccount.account_number_blind_index == blind_index,
            EmployeeBankAccount.organization_id.in_(organization_ids),
        )
        return self._session.execute(statement).scalars().first()


__all__ = ["EmployeeBankAccountRepository"]
