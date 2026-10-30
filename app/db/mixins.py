"""Reusable column groups: the §1.5 standard columns, organization scoping, archival.

Three mixins, matching the three table shapes the spec describes:

* :class:`StandardColumns` — "every business table includes the following columns" (§1.5).
* :class:`AppendOnlyColumns` — "append-only tables (audit logs, history/event tables, code
  registry) have only ``id``, ``created_at``, ``created_by`` and are never updated" (§1.5).
* :class:`OrganizationScoped` — the tenant discriminator plus the ``UNIQUE (organization_id,
  id)`` that §23.1 requires on every organization-scoped table so it can be the target of a
  composite foreign key.

Optimistic locking is wired here once: :class:`StandardColumns` sets SQLAlchemy's
``version_id_col``, so every UPDATE carries ``WHERE row_version = :loaded`` and a lost update
raises ``StaleDataError`` at flush time instead of silently overwriting. §20.4 and §24.1
expose the same counter as an HTTP ``ETag``.

Composite organization foreign keys are **not** wrapped in a helper here. SQLAlchemy
expresses them as a ``ForeignKeyConstraint`` in a model's ``__table_args__`` rather than as a
column-level ``ForeignKey``, and hiding that behind a factory would obscure which pair of
columns is actually constrained. Models declare them explicitly; see
:mod:`app.models.employee` for the canonical examples.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, declared_attr, mapped_column

from app.db.types import Uuid7, uuid7

ARCHIVE_REASON_MIN_LENGTH = 10
ARCHIVE_REASON_MAX_LENGTH = 500
"""§1.5: ``archive_reason`` is required, 10-500 characters, when ``archived_at`` is set."""


class PrimaryKeyColumn:
    """The immutable UUID primary key (§1.5).

    Never reused and never exposed as a business identifier — ``employee_code`` and the
    master ``code`` columns are the human-readable identifiers.
    """

    @declared_attr
    def id(self) -> Mapped[uuid.UUID]:
        return mapped_column(Uuid7, primary_key=True, default=uuid7, nullable=False)


class CreatedColumns:
    """Creation audit columns, shared by business and append-only tables."""

    @declared_attr
    def created_at(self) -> Mapped[datetime]:
        return mapped_column(
            nullable=False,
            server_default=text("CURRENT_TIMESTAMP(6)"),
        )

    @declared_attr
    def created_by(self) -> Mapped[uuid.UUID | None]:
        # Nullable so the very first platform user and the seed process can create rows
        # before any user exists; every application-initiated write sets it.
        return mapped_column(
            Uuid7, ForeignKey("users.id", ondelete="RESTRICT", name=None), nullable=True
        )


class StandardColumns(PrimaryKeyColumn, CreatedColumns):
    """The full §1.5 standard column set for a business table."""

    @declared_attr
    def updated_at(self) -> Mapped[datetime]:
        return mapped_column(
            nullable=False,
            server_default=text("CURRENT_TIMESTAMP(6)"),
            server_onupdate=text("CURRENT_TIMESTAMP(6)"),
        )

    @declared_attr
    def updated_by(self) -> Mapped[uuid.UUID | None]:
        return mapped_column(Uuid7, ForeignKey("users.id", ondelete="RESTRICT"), nullable=True)

    @declared_attr
    def row_version(self) -> Mapped[int]:
        """§1.5: starts at 1, incremented on every update. Exposed as the HTTP ETag."""
        return mapped_column(Integer, nullable=False, default=1, server_default=text("1"))

    @declared_attr
    def archived_at(self) -> Mapped[datetime | None]:
        """Soft archival (§23.4). Archived rows stay out of default queries."""
        return mapped_column(nullable=True, default=None)

    @declared_attr
    def archived_by(self) -> Mapped[uuid.UUID | None]:
        return mapped_column(Uuid7, ForeignKey("users.id", ondelete="RESTRICT"), nullable=True)

    @declared_attr
    def archive_reason(self) -> Mapped[str | None]:
        return mapped_column(String(ARCHIVE_REASON_MAX_LENGTH), nullable=True)

    @declared_attr.directive
    def __mapper_args__(cls) -> dict[str, Any]:  # noqa: N805 — SQLAlchemy directive signature
        """Enable SQLAlchemy's optimistic concurrency control on ``row_version``."""
        return {"version_id_col": cls.__table__.c.row_version}  # type: ignore[attr-defined]

    @property
    def is_archived(self) -> bool:
        return self.archived_at is not None


class AppendOnlyColumns(PrimaryKeyColumn, CreatedColumns):
    """§1.5: append-only tables carry only ``id``, ``created_at`` and ``created_by``.

    Deliberately has no ``row_version``, ``updated_*`` or ``archived_*``: these tables are
    never updated, so giving them update machinery would invite exactly the writes §20.1
    forbids ("the application database role has INSERT and SELECT only").
    """


class OrganizationScoped:
    """The tenant discriminator (§6.1) and its composite-FK support (§23.1).

    ``UNIQUE (organization_id, id)`` is redundant as a uniqueness claim — ``id`` is already
    the primary key — but it is what makes ``FOREIGN KEY (organization_id, x_id) REFERENCES
    x (organization_id, id)`` possible. Those composite FKs are layer 4 of the isolation
    model: the database itself refuses a child row whose parent is in another organization.
    """

    @declared_attr
    def organization_id(self) -> Mapped[uuid.UUID]:
        return mapped_column(
            Uuid7,
            ForeignKey("organizations.id", ondelete="RESTRICT"),
            nullable=False,
            index=True,
        )

    @declared_attr.directive
    def __table_args__(cls) -> tuple[Any, ...]:  # noqa: N805 — SQLAlchemy directive signature
        return org_scope_table_args(cls.__tablename__)  # type: ignore[attr-defined]


def org_scope_table_args(table_name: str) -> tuple[Any, ...]:
    """The table arguments every organization-scoped table needs.

    Exposed as a function because most models declare additional constraints and must
    therefore spell out ``__table_args__`` themselves; they splat this in rather than
    re-deriving the composite-FK target constraint and risking omitting it.
    """
    return (UniqueConstraint("organization_id", "id", name=f"uq_{table_name}_org_id"),)


def archive_reason_constraint(table_name: str) -> CheckConstraint:
    """§1.5: ``archive_reason`` is mandatory, 10-500 characters, exactly when archived.

    Expressed as a database CHECK rather than only in a service, so a row cannot be archived
    without a reason by any path, including a manual fix-up.
    """
    return CheckConstraint(
        "(archived_at IS NULL AND archive_reason IS NULL) OR "
        "(archived_at IS NOT NULL AND archive_reason IS NOT NULL AND "
        f"CHAR_LENGTH(archive_reason) BETWEEN {ARCHIVE_REASON_MIN_LENGTH} "
        f"AND {ARCHIVE_REASON_MAX_LENGTH})",
        name=f"ck_{table_name}_archive_reason",
    )


def updated_after_created_constraint(table_name: str) -> CheckConstraint:
    """§1.5: ``updated_at >= created_at``."""
    return CheckConstraint(
        "updated_at >= created_at", name=f"ck_{table_name}_updated_after_created"
    )


def status_constraint(table_name: str, column: str = "status") -> CheckConstraint:
    """The ubiquitous ``ACTIVE``/``INACTIVE`` status check (§7.2, §8.1, §9.1, ...)."""
    return CheckConstraint(f"{column} IN ('ACTIVE','INACTIVE')", name=f"ck_{table_name}_{column}")


def tenant_index(table_name: str, *columns: str, name: str | None = None) -> Index:
    """A tenant-scoped index, always leading with ``organization_id``.

    Every list, search, filter and sort in this system is organization-scoped (§26.1), so an
    index that does not lead with the discriminator cannot serve those queries.
    """
    index_name = name or f"ix_{table_name}_{'_'.join(columns)}"
    if len(index_name) > 64:
        raise ValueError(f"index name exceeds the MySQL 64-character limit: {index_name}")
    return Index(index_name, "organization_id", *columns)


__all__ = [
    "ARCHIVE_REASON_MAX_LENGTH",
    "ARCHIVE_REASON_MIN_LENGTH",
    "AppendOnlyColumns",
    "CreatedColumns",
    "OrganizationScoped",
    "PrimaryKeyColumn",
    "StandardColumns",
    "archive_reason_constraint",
    "org_scope_table_args",
    "status_constraint",
    "tenant_index",
    "updated_after_created_constraint",
]
