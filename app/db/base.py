"""Declarative base and naming conventions.

A fixed naming convention matters more than usual here: Alembic autogenerate names
constraints from it, and the §23.3 constraint catalogue is large enough that
auto-generated anonymous names would make migrations unreviewable and downgrades
unreliable.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import JSON, MetaData
from sqlalchemy.orm import DeclarativeBase, registry

from app.db.types import UtcDateTime, Uuid7, money

# No "ck" entry: SQLAlchemy would substitute an explicitly given CHECK name into
# "ck_%(table_name)s_%(constraint_name)s" and double the prefix. Every CHECK in this
# schema carries its full `ck_<table>_<rule>` name at the point of definition instead,
# which also keeps the name greppable from the constraint it enforces.
NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s",
    "pk": "pk_%(table_name)s",
}

# MySQL caps identifiers at 64 characters. Long composite index names are therefore given
# explicit short names at the point of definition rather than relying on the convention.
MYSQL_MAX_IDENTIFIER_LENGTH = 64

metadata_obj = MetaData(naming_convention=NAMING_CONVENTION)


class Base(DeclarativeBase):
    """Base class for every ORM model.

    ``type_annotation_map`` lets models declare plain Python annotations
    (``Mapped[uuid.UUID]``, ``Mapped[datetime]``, ``Mapped[Decimal]``) and get the §1.4
    logical types without repeating the column type on every one of several hundred columns.
    """

    metadata = metadata_obj
    registry = registry(
        metadata=metadata_obj,
        type_annotation_map={
            uuid.UUID: Uuid7,
            datetime: UtcDateTime,
            # A bare `Decimal` annotation means a monetary reference value. Percentage and
            # share columns are narrower, so they name their type explicitly.
            Decimal: money(),
            dict[str, Any]: JSON,
            list[Any]: JSON,
        },
    )

    def __repr__(self) -> str:
        """Identity-only representation.

        Deliberately excludes column values: models carry personal data and sensitive
        identifiers, and a default repr is exactly how those end up in a log line or a
        traceback (§25.9).
        """
        identifier = getattr(self, "id", None)
        return f"<{type(self).__name__} id={identifier}>"


__all__ = ["MYSQL_MAX_IDENTIFIER_LENGTH", "NAMING_CONVENTION", "Base", "metadata_obj"]
