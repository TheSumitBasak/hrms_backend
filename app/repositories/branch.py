"""Reads and writes for branches (§8)."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select

from app.models.branch import Branch
from app.repositories.base import OrgScopedRepository
from app.validation.normalizers import escape_like_pattern


class BranchRepository(OrgScopedRepository[Branch]):
    model = Branch

    def get_by_code(self, branch_code: str) -> Branch | None:
        statement = self.select().where(
            Branch.branch_code_normalized == branch_code.strip().upper()
        )
        return self._session.execute(statement).scalar_one_or_none()

    def list_branches(
        self,
        *,
        status: str | None = None,
        query: str | None = None,
    ) -> Sequence[Branch]:
        statement = self.select()
        if status is not None:
            statement = statement.where(Branch.status == status)
        if query:
            pattern = f"%{escape_like_pattern(query)}%"
            statement = statement.where(
                Branch.branch_name.like(pattern)
                | Branch.branch_code_normalized.like(pattern.upper())
            )
        statement = statement.order_by(Branch.branch_name, Branch.id)
        return list(self._session.execute(statement).scalars())

    def name_exists(self, branch_name: str) -> bool:
        statement = (
            select(Branch.id)
            .where(
                Branch.organization_id == self.organization_id,
                Branch.archived_at.is_(None),
                Branch.branch_name == branch_name.strip(),
            )
            .limit(1)
        )
        return self._session.execute(statement).first() is not None


__all__ = ["BranchRepository"]
