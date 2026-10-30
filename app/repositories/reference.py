"""Platform reference data: Indian states and GST state codes (§23.2)."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import func, select

from app.models.reference import State
from app.repositories.base import Repository


class StateRepository(Repository):
    """The ``states`` reference list.

    Backs the §7.1 rule that an Indian address must match a known state, and the derivation of
    ``state_code`` from that state. Seeded rather than hardcoded (ADR-006), so the list is
    configuration.
    """

    def get(self, code: str) -> State | None:
        return self._session.get(State, code)

    def get_by_name(self, name: str) -> State | None:
        """Look up by name, case-insensitively and ignoring surrounding whitespace.

        Clients send a human-entered state name, so an exact match would reject "gujarat" and
        " Gujarat ". The comparison is deliberately forgiving about case and padding and
        strict about everything else: a misspelling is still a validation error.
        """
        cleaned = (name or "").strip()
        if not cleaned:
            return None
        statement = select(State).where(func.lower(State.name) == cleaned.lower())
        return self._session.execute(statement).scalar_one_or_none()

    def list_active(self) -> Sequence[State]:
        statement = select(State).where(State.status == "ACTIVE").order_by(State.name)
        return list(self._session.execute(statement).scalars())

    def code_exists(self, code: str) -> bool:
        return self.get(code) is not None


__all__ = ["StateRepository"]
