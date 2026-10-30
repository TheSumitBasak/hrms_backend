"""Data access, and the layer where tenant isolation is structurally guaranteed.

MySQL has no row-level security, so §23.6 prescribes the alternative: *"equivalent filter
enforced in a mandatory repository layer; automated tests attempt cross-organization access for
every endpoint"*. This module is that mandatory layer.

The guarantee is made structural rather than conventional:

* :class:`OrgScopedRepository` **cannot be constructed** without an organization id. There is
  no default, no ``None`` fallback and no setter.
* Every query it builds starts from :meth:`OrgScopedRepository.select`, which applies
  ``WHERE organization_id = :context`` before the caller sees it.
* Services never build organization-scoped queries by hand. A service that wants a different
  filter adds a method here, where the tenant predicate is already applied.

Repositories never commit. Transaction boundaries belong to the service's
:func:`app.db.session.unit_of_work`, so a business change and its audit row commit together
(NFR-10).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any, ClassVar

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.core.errors import ResourceNotFound
from app.db.base import Base


class Repository:
    """Base for platform-level tables that are not organization-scoped.

    Only for genuinely cross-tenant data: ``users``, ``roles``, ``permissions``, ``states``,
    ``identifier_type_configs``. Anything carrying an ``organization_id`` belongs in
    :class:`OrgScopedRepository` instead, and the test suite asserts that split.
    """

    __slots__ = ("_session",)

    def __init__(self, session: Session) -> None:
        self._session = session

    @property
    def session(self) -> Session:
        return self._session

    def flush(self) -> None:
        """Push pending changes so server-side defaults and generated columns are readable.

        Not a commit. Needed when a service must read back a generated value — an
        ``employee_code_normalized``, a ``row_version`` — before the transaction ends.
        """
        self._session.flush()


class OrgScopedRepository[ModelT: Base](Repository):
    """Base for every organization-scoped table (§6.1 layer 3).

    Subclasses set :attr:`model`. The organization id is supplied once at construction and
    applied to every query thereafter.
    """

    model: ClassVar[type[Any]]
    """The mapped class this repository reads and writes."""

    __slots__ = ("_organization_id",)

    def __init__(self, session: Session, organization_id: uuid.UUID) -> None:
        if organization_id is None:  # pragma: no cover - defensive, typing forbids it
            raise ValueError(
                "OrgScopedRepository requires an organization id: an unscoped query over an "
                "organization-scoped table would cross the tenant boundary (§6.1)"
            )
        if not hasattr(type(self), "model"):
            raise TypeError(f"{type(self).__name__} must define a `model` class attribute")
        super().__init__(session)
        self._organization_id = organization_id

    @property
    def organization_id(self) -> uuid.UUID:
        return self._organization_id

    # ---------------------------------------------------------------- queries

    def select(self, *, include_archived: bool = False) -> Select[tuple[ModelT]]:
        """A tenant-filtered ``SELECT`` over :attr:`model`.

        The single entry point for reads. ``include_archived`` defaults to false because §23.4
        excludes archived rows from default queries; a caller that needs them must ask, and
        §26.2 gates that behind ``employee.archive``.
        """
        statement = select(self.model).where(self.model.organization_id == self._organization_id)
        if not include_archived and hasattr(self.model, "archived_at"):
            statement = statement.where(self.model.archived_at.is_(None))
        return statement

    def get(self, entity_id: uuid.UUID, *, include_archived: bool = False) -> ModelT | None:
        """Fetch one row by id **within this organization**, or ``None``.

        A row that exists in another organization returns ``None`` here, which the caller
        surfaces as ``404 RESOURCE_NOT_FOUND`` — §24.3's no-existence-disclosure rule.
        """
        statement = self.select(include_archived=include_archived).where(self.model.id == entity_id)
        return self._session.execute(statement).scalar_one_or_none()

    def require(
        self,
        entity_id: uuid.UUID,
        *,
        include_archived: bool = False,
        resource: str | None = None,
    ) -> ModelT:
        """Fetch one row or raise ``404 RESOURCE_NOT_FOUND``.

        The message is identical whether the row is absent or belongs to another organization
        (§24.3), so probing reveals nothing.
        """
        found = self.get(entity_id, include_archived=include_archived)
        if found is None:
            raise ResourceNotFound(resource or self.model.__tablename__.rstrip("s"))
        return found

    def exists(self, entity_id: uuid.UUID, *, include_archived: bool = False) -> bool:
        statement = (
            self.select(include_archived=include_archived)
            .where(self.model.id == entity_id)
            .limit(1)
        )
        return self._session.execute(statement).first() is not None

    def count(self, *, include_archived: bool = False) -> int:
        statement = (
            select(func.count())
            .select_from(self.model)
            .where(self.model.organization_id == self._organization_id)
        )
        if not include_archived and hasattr(self.model, "archived_at"):
            statement = statement.where(self.model.archived_at.is_(None))
        return int(self._session.execute(statement).scalar() or 0)

    def list_all(self, *, include_archived: bool = False) -> Sequence[ModelT]:
        """Every row in this organization.

        For bounded configuration tables only — masters, lookups, document categories. Never
        for employees or documents, which §18 of the brief requires to be paginated.
        """
        return list(self._session.execute(self.select(include_archived=include_archived)).scalars())

    # ---------------------------------------------------------------- locking

    def lock(self, entity_id: uuid.UUID) -> ModelT:
        """Fetch one row with ``SELECT … FOR UPDATE``, serialising writes against it.

        §18.3 and §19.6 both build on this. Callers must respect the documented lock order —
        ``employees`` before ``employee_salary_assignments`` — or risk a deadlock, which
        surfaces as a retryable ``409 CONCURRENT_MODIFICATION``.
        """
        statement = (
            self.select(include_archived=True).where(self.model.id == entity_id).with_for_update()
        )
        found = self._session.execute(statement).scalar_one_or_none()
        if found is None:
            raise ResourceNotFound(self.model.__tablename__.rstrip("s"))
        return found

    # ---------------------------------------------------------------- writes

    def add(self, entity: ModelT) -> ModelT:
        """Stage a new row, asserting it belongs to this repository's organization.

        The assertion catches the mistake this layer exists to prevent: a service building an
        entity with an organization id taken from a request body rather than from the verified
        path context (§7 of the brief: "Do not trust an organization ID supplied in a request
        body").
        """
        entity_organization = getattr(entity, "organization_id", None)
        if entity_organization is None:
            entity.organization_id = self._organization_id  # type: ignore[attr-defined]
        elif entity_organization != self._organization_id:
            raise ValueError(
                "refusing to add a row whose organization_id differs from the request "
                "context; this would cross the tenant boundary (§6.3)"
            )
        self._session.add(entity)
        return entity

    def add_all(self, entities: Sequence[ModelT]) -> Sequence[ModelT]:
        for entity in entities:
            self.add(entity)
        return entities

    def delete(self, entity: ModelT) -> None:
        """Hard-delete a row.

        §23.4 limits hard deletion to never-referenced configuration rows, never-submitted
        salary drafts, ``PENDING_UPLOAD`` documents and retention purges. Business records are
        archived instead, so most repositories should not expose a path to this.
        """
        if getattr(entity, "organization_id", None) != self._organization_id:
            raise ValueError("refusing to delete a row from another organization")
        self._session.delete(entity)


__all__ = ["OrgScopedRepository", "Repository"]
