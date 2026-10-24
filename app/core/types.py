"""Portable column types that degrade gracefully on non-PostgreSQL backends."""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import JSON
from sqlalchemy.types import TypeDecorator

try:  # pragma: no cover - import guard
    from sqlalchemy.dialects.postgresql import JSONB as _PG_JSONB
except Exception:  # pragma: no cover
    _PG_JSONB = None


class JSONType(TypeDecorator):
    """JSON column that adapts to the backend.

    Uses the native MySQL ``JSON`` type and PostgreSQL ``JSONB`` when available,
    falling back to plain ``JSON`` on SQLite for tests.
    """

    impl = JSON
    cache_ok = True

    def load_dialect_impl(self, dialect):  # noqa: ANN001
        if dialect.name == "postgresql" and _PG_JSONB is not None:
            return dialect.type_descriptor(_PG_JSONB())
        return dialect.type_descriptor(JSON())

    def process_bind_param(self, value: Any, dialect):  # noqa: ANN001
        return value

    def process_result_value(self, value: Any, dialect):  # noqa: ANN001
        if value is None:
            return None
        if isinstance(value, str):
            try:
                return json.loads(value)
            except (TypeError, ValueError):
                return value
        return value
