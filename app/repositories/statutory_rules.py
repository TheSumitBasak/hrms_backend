"""Platform-level repositories for the statutory rule framework (payroll addendum §4.6)."""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select

from app.core.errors import ResourceNotFound
from app.models.statutory_rules import (
    StatutoryRuleListValue,
    StatutoryRuleParameter,
    StatutoryRuleSet,
    StatutoryRuleSlab,
    StatutoryRuleVersion,
)
from app.repositories.base import Repository


@dataclass
class VersionContents:
    parameters: list[StatutoryRuleParameter] = field(default_factory=list)
    slabs: list[StatutoryRuleSlab] = field(default_factory=list)
    list_values: list[StatutoryRuleListValue] = field(default_factory=list)


class StatutoryRuleRepository(Repository):
    """Rule sets, versions and their children. Not organization-scoped: the rules are law."""

    def list_sets(
        self, *, family: str | None = None, jurisdiction_code: str | None = None
    ) -> Sequence[StatutoryRuleSet]:
        statement = select(StatutoryRuleSet).where(StatutoryRuleSet.archived_at.is_(None))
        if family is not None:
            statement = statement.where(StatutoryRuleSet.rule_family == family)
        if jurisdiction_code is not None:
            statement = statement.where(StatutoryRuleSet.jurisdiction_code == jurisdiction_code)
        statement = statement.order_by(
            StatutoryRuleSet.rule_family,
            StatutoryRuleSet.jurisdiction_code,
            StatutoryRuleSet.rule_set_code,
        )
        return list(self._session.execute(statement).scalars())

    def require_set(self, set_id: uuid.UUID, *, lock: bool = False) -> StatutoryRuleSet:
        statement = select(StatutoryRuleSet).where(StatutoryRuleSet.id == set_id)
        if lock:
            statement = statement.with_for_update()
        found = self._session.execute(statement).scalar_one_or_none()
        if found is None:
            raise ResourceNotFound("statutory rule set")
        return found

    def require_version(self, version_id: uuid.UUID) -> StatutoryRuleVersion:
        found = self._session.get(StatutoryRuleVersion, version_id)
        if found is None:
            raise ResourceNotFound("statutory rule version")
        return found

    def published_versions(self, set_ids: Sequence[uuid.UUID]) -> Sequence[StatutoryRuleVersion]:
        statement = (
            select(StatutoryRuleVersion)
            .where(
                StatutoryRuleVersion.rule_set_id.in_(set_ids),
                StatutoryRuleVersion.status == "PUBLISHED",
            )
            .order_by(StatutoryRuleVersion.rule_set_id, StatutoryRuleVersion.effective_from)
        )
        return list(self._session.execute(statement).scalars())

    def next_version_number(self, set_id: uuid.UUID) -> int:
        statement = select(func.max(StatutoryRuleVersion.version_number)).where(
            StatutoryRuleVersion.rule_set_id == set_id
        )
        return int(self._session.execute(statement).scalar() or 0) + 1

    def contents(self, version_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, VersionContents]:
        """Children of several versions in three queries, not three per version."""
        result: dict[uuid.UUID, VersionContents] = defaultdict(VersionContents)
        if not version_ids:
            return result
        children: tuple[tuple[Any, str], ...] = (
            (StatutoryRuleParameter, "parameters"),
            (StatutoryRuleSlab, "slabs"),
            (StatutoryRuleListValue, "list_values"),
        )
        for model, attribute in children:
            rows = self._session.execute(
                select(model).where(model.rule_version_id.in_(version_ids)).order_by(model.id)
            ).scalars()
            for row in rows:
                getattr(result[row.rule_version_id], attribute).append(row)
        return result

    def add(self, *entities: object) -> None:
        self._session.add_all(entities)
