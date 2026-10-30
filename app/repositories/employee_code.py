"""Reads and writes for employee ID generation (§19)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.mysql import insert

from app.core.clock import utc_now
from app.models.employee_code import (
    CodeRegistryStatus,
    EmployeeCodeRegistry,
    EmployeeIdGenerationRule,
    EmployeeIdSequence,
    IdRuleStatus,
)
from app.repositories.base import OrgScopedRepository


class IdRuleRepository(OrgScopedRepository[EmployeeIdGenerationRule]):
    model = EmployeeIdGenerationRule

    def list_rules(
        self,
        *,
        department_id: uuid.UUID | None = None,
        status: Sequence[str] | None = None,
    ) -> Sequence[EmployeeIdGenerationRule]:
        statement = self.select()
        if department_id is not None:
            statement = statement.where(
                (EmployeeIdGenerationRule.department_id == department_id)
                | EmployeeIdGenerationRule.department_id.is_(None)
            )
        if status:
            statement = statement.where(EmployeeIdGenerationRule.status.in_(status))
        statement = statement.order_by(
            EmployeeIdGenerationRule.priority,
            EmployeeIdGenerationRule.effective_from.desc(),
            EmployeeIdGenerationRule.rule_code,
        )
        return list(self._session.execute(statement).scalars())

    def get_by_code(self, rule_code: str) -> EmployeeIdGenerationRule | None:
        statement = self.select().where(
            EmployeeIdGenerationRule.rule_code_normalized == rule_code.strip().upper()
        )
        return self._session.execute(statement).scalar_one_or_none()

    def unarchived_rules_for_department(
        self, department_id: uuid.UUID
    ) -> Sequence[EmployeeIdGenerationRule]:
        statement = self.select().where(
            EmployeeIdGenerationRule.department_id == department_id,
            EmployeeIdGenerationRule.status != IdRuleStatus.ARCHIVED.value,
        )
        return list(self._session.execute(statement).scalars())


class EmployeeCodeRegistryRepository(OrgScopedRepository[EmployeeCodeRegistry]):
    model = EmployeeCodeRegistry

    def get_by_code(self, code: str) -> EmployeeCodeRegistry | None:
        statement = self.select(include_archived=True).where(
            EmployeeCodeRegistry.employee_code_normalized == code.strip().upper()
        )
        return self._session.execute(statement).scalar_one_or_none()

    def count_for_rule(self, rule_id: uuid.UUID) -> int:
        statement = (
            select(func.count())
            .select_from(EmployeeCodeRegistry)
            .where(
                EmployeeCodeRegistry.organization_id == self.organization_id,
                EmployeeCodeRegistry.rule_id == rule_id,
            )
        )
        return int(self._session.execute(statement).scalar() or 0)

    def list_for_employee(self, employee_id: uuid.UUID) -> Sequence[EmployeeCodeRegistry]:
        statement = (
            self.select(include_archived=True)
            .where(EmployeeCodeRegistry.employee_id == employee_id)
            .order_by(EmployeeCodeRegistry.issued_at)
        )
        return list(self._session.execute(statement).scalars())

    def search(
        self,
        *,
        code: str | None = None,
        employee_id: uuid.UUID | None = None,
        status: str | None = None,
    ) -> Sequence[EmployeeCodeRegistry]:
        statement = self.select(include_archived=True)
        if code:
            statement = statement.where(
                EmployeeCodeRegistry.employee_code_normalized == code.strip().upper()
            )
        if employee_id is not None:
            statement = statement.where(EmployeeCodeRegistry.employee_id == employee_id)
        if status is not None:
            statement = statement.where(EmployeeCodeRegistry.status == status)
        statement = statement.order_by(EmployeeCodeRegistry.issued_at.desc())
        return list(self._session.execute(statement).scalars())

    def retire(self, code: EmployeeCodeRegistry, *, reason: str, user_id: uuid.UUID) -> None:
        code.status = CodeRegistryStatus.RETIRED.value
        code.retired_reason = reason
        code.retired_at = utc_now()
        code.retired_by = user_id


class EmployeeIdSequenceRepository(OrgScopedRepository[EmployeeIdSequence]):
    """Sequence counters are organization-scoped but have no archive column."""

    model = EmployeeIdSequence

    def list_sequences(self) -> Sequence[EmployeeIdSequence]:
        statement = self.select(include_archived=True).order_by(
            EmployeeIdSequence.sequence_group_code, EmployeeIdSequence.scope_key
        )
        return list(self._session.execute(statement).scalars())

    def find(self, sequence_group_code: str, scope_key: str) -> EmployeeIdSequence | None:
        statement = self.select(include_archived=True).where(
            EmployeeIdSequence.sequence_group_code == sequence_group_code,
            EmployeeIdSequence.scope_key == scope_key,
        )
        return self._session.execute(statement).scalar_one_or_none()

    def acquire_named_lock(self, name: str, *, timeout: int = 10) -> None:
        result = self._session.execute(
            text("SELECT GET_LOCK(:name, :timeout)"),
            {"name": name, "timeout": timeout},
        ).scalar()
        if result != 1:
            raise RuntimeError("could not acquire the employee sequence lock")

    def allocate_next(
        self,
        *,
        sequence_group_code: str,
        scope_key: str,
        sequence_start: int,
        sequence_increment: int,
    ) -> int:
        """Atomically reserve the next value with one UPDATE (no gap-lock read)."""
        for _ in range(2):
            result = self._session.execute(
                update(EmployeeIdSequence)
                .where(
                    EmployeeIdSequence.organization_id == self.organization_id,
                    EmployeeIdSequence.sequence_group_code == sequence_group_code,
                    EmployeeIdSequence.scope_key == scope_key,
                )
                .values(last_issued_value=EmployeeIdSequence.last_issued_value + sequence_increment)
            )
            if result.rowcount:  # type: ignore[attr-defined]
                return int(
                    self._session.execute(
                        select(EmployeeIdSequence.last_issued_value).where(
                            EmployeeIdSequence.organization_id == self.organization_id,
                            EmployeeIdSequence.sequence_group_code == sequence_group_code,
                            EmployeeIdSequence.scope_key == scope_key,
                        )
                    ).scalar_one()
                )
            self._session.execute(
                insert(EmployeeIdSequence)
                .prefix_with("IGNORE")
                .values(
                    id=uuid.uuid4(),
                    organization_id=self.organization_id,
                    sequence_group_code=sequence_group_code,
                    scope_key=scope_key,
                    last_issued_value=sequence_start - sequence_increment,
                )
            )
        raise RuntimeError("could not allocate an employee sequence value")

    def release_named_lock(self, name: str) -> None:
        self._session.execute(text("SELECT RELEASE_LOCK(:name)"), {"name": name})

    def ensure_locked(
        self,
        *,
        sequence_group_code: str,
        scope_key: str,
        sequence_start: int,
        sequence_increment: int,
    ) -> EmployeeIdSequence:
        """Create the counter if absent, then lock it (§19.6 step 3).

        ``INSERT IGNORE`` makes the create race-free; the subsequent ``FOR UPDATE`` is what
        serialises concurrent issuers so AC-16 produces contiguous codes.
        """
        self._session.execute(
            insert(EmployeeIdSequence)
            .prefix_with("IGNORE")
            .values(
                id=uuid.uuid4(),
                organization_id=self.organization_id,
                sequence_group_code=sequence_group_code,
                scope_key=scope_key,
                last_issued_value=sequence_start - sequence_increment,
            )
        )
        locked = self.lock_for_update(sequence_group_code, scope_key)
        if locked is None:  # pragma: no cover - INSERT IGNORE guarantees a row
            raise RuntimeError("employee ID sequence row disappeared")
        return locked

    def lock_for_update(
        self, sequence_group_code: str, scope_key: str
    ) -> EmployeeIdSequence | None:
        statement = (
            self.select(include_archived=True)
            .where(
                EmployeeIdSequence.sequence_group_code == sequence_group_code,
                EmployeeIdSequence.scope_key == scope_key,
            )
            .with_for_update()
        )
        return self._session.execute(statement).scalar_one_or_none()


__all__ = [
    "EmployeeCodeRegistryRepository",
    "EmployeeIdSequenceRepository",
    "IdRuleRepository",
]
