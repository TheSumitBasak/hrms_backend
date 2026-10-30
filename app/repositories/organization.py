"""Reads and writes for organizations and organization groups (§7).

``organizations`` is the tenant root, so it extends :class:`~app.repositories.base.Repository`
rather than the tenant-scoped base: a repository that filtered organizations *by* organization
would be circular. Access control for these rows is therefore the caller's responsibility —
every method that could return an organization the caller cannot see takes the set of
accessible ids explicitly, so forgetting to scope is a missing argument rather than a silent
leak.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import func, select

from app.core.errors import ResourceNotFound
from app.models.org_config import (
    IdentifierRequirementPolicy,
    NominationTypeConfig,
    OrganizationRegistration,
)
from app.models.organization import Organization, OrganizationGroup, OrganizationSettings
from app.repositories.base import OrgScopedRepository, Repository
from app.validation.normalizers import escape_like_pattern


class OrganizationRepository(Repository):
    """Organization profiles."""

    def get(self, organization_id: uuid.UUID) -> Organization | None:
        return self._session.get(Organization, organization_id)

    def get_by_code(self, organization_code: str) -> Organization | None:
        """Look up by code. Codes are compared case-insensitively (§7.2)."""
        statement = select(Organization).where(
            Organization.organization_code_normalized == organization_code.upper()
        )
        return self._session.execute(statement).scalar_one_or_none()

    def get_many(self, organization_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, Organization]:
        """Fetch several organizations by id in one query.

        Used when building a caller's access summary: one query for every organization they
        can reach, rather than one per grant (§17 of the brief — no N+1).
        """
        if not organization_ids:
            return {}
        statement = select(Organization).where(Organization.id.in_(organization_ids))
        return {row.id: row for row in self._session.execute(statement).scalars()}

    def list_accessible(
        self,
        accessible_ids: Sequence[uuid.UUID],
        *,
        status: str | None = None,
        group_id: uuid.UUID | None = None,
        query: str | None = None,
        limit: int = 100,
    ) -> Sequence[Organization]:
        """``GET /orgs`` — organizations the caller holds a grant in (§24.4.1).

        ``accessible_ids`` is mandatory and never defaulted: the endpoint lists "organizations
        accessible to caller", and an empty list must mean "none" rather than "all".
        """
        if not accessible_ids:
            return []
        statement = select(Organization).where(
            Organization.id.in_(accessible_ids), Organization.archived_at.is_(None)
        )
        if status is not None:
            statement = statement.where(Organization.status == status)
        if group_id is not None:
            statement = statement.where(Organization.group_id == group_id)
        if query:
            pattern = f"%{escape_like_pattern(query)}%"
            statement = statement.where(
                Organization.display_name.like(pattern)
                | Organization.legal_name.like(pattern)
                | Organization.organization_code_normalized.like(
                    f"{escape_like_pattern(query).upper()}%"
                )
            )
        statement = statement.order_by(Organization.display_name, Organization.id).limit(limit)
        return list(self._session.execute(statement).scalars())

    def code_exists(self, organization_code: str) -> bool:
        statement = (
            select(Organization.id)
            .where(Organization.organization_code_normalized == organization_code.upper())
            .limit(1)
        )
        return self._session.execute(statement).first() is not None

    def list_by_legal_name(self, legal_name: str) -> Sequence[Organization]:
        """Organizations sharing a legal name, for the §7.2 ``W-ORG-NAME`` warning.

        A duplicate legal name is a warning rather than a conflict: group companies
        legitimately share a trading name, so the system flags it and lets an
        administrator confirm.
        """
        statement = select(Organization).where(
            func.lower(Organization.legal_name) == legal_name.strip().lower(),
            Organization.archived_at.is_(None),
        )
        return list(self._session.execute(statement).scalars())

    def pan_exists(self, pan: str) -> bool:
        """Whether any organization already records this PAN (§7.2)."""
        statement = (
            select(Organization.id)
            .where(Organization.pan == pan.upper(), Organization.archived_at.is_(None))
            .limit(1)
        )
        return self._session.execute(statement).first() is not None

    def add(self, organization: Organization) -> Organization:
        self._session.add(organization)
        return organization

    def group_ids_for(
        self, organization_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, uuid.UUID | None]:
        """Map each organization to its group, for the §11.5 group-scoped duplicate check.

        The group is also the blind-index key scope (§23.7), so this is what decides whether
        two organizations' identifier digests can match at all.
        """
        if not organization_ids:
            return {}
        statement = select(Organization.id, Organization.group_id).where(
            Organization.id.in_(organization_ids)
        )
        return {row.id: row.group_id for row in self._session.execute(statement).all()}

    def organizations_in_group(self, group_id: uuid.UUID) -> Sequence[uuid.UUID]:
        """Every organization in one group (§11.5, §10.5 GROUP email scope)."""
        statement = select(Organization.id).where(
            Organization.group_id == group_id, Organization.archived_at.is_(None)
        )
        return list(self._session.execute(statement).scalars())


class OrganizationSettingsRepository(Repository):
    """Per-organization configuration (§7.5).

    Read on nearly every write, because the settings decide which fields are collected, how
    codes are assigned, whether approvals are required and how far back a salary revision may
    be dated.
    """

    def get(self, organization_id: uuid.UUID) -> OrganizationSettings | None:
        return self._session.get(OrganizationSettings, organization_id)

    def require(self, organization_id: uuid.UUID) -> OrganizationSettings:
        """Settings must exist: organization creation seeds them in the same transaction."""
        found = self.get(organization_id)
        if found is None:
            raise ResourceNotFound("organization settings")
        return found

    def add(self, settings: OrganizationSettings) -> OrganizationSettings:
        self._session.add(settings)
        return settings


class OrganizationGroupRepository(Repository):
    """Organization groups (§7.6)."""

    def get(self, group_id: uuid.UUID) -> OrganizationGroup | None:
        return self._session.get(OrganizationGroup, group_id)

    def get_by_code(self, group_code: str) -> OrganizationGroup | None:
        statement = select(OrganizationGroup).where(
            OrganizationGroup.group_code == group_code.upper()
        )
        return self._session.execute(statement).scalar_one_or_none()

    def list_all(self) -> Sequence[OrganizationGroup]:
        statement = (
            select(OrganizationGroup)
            .where(OrganizationGroup.archived_at.is_(None))
            .order_by(OrganizationGroup.group_name)
        )
        return list(self._session.execute(statement).scalars())

    def add(self, group: OrganizationGroup) -> OrganizationGroup:
        self._session.add(group)
        return group


class OrganizationRegistrationRepository(OrgScopedRepository[OrganizationRegistration]):
    """Statutory registrations of one organization (§7.4)."""

    model = OrganizationRegistration

    def list_registrations(
        self, *, registration_type: str | None = None
    ) -> Sequence[OrganizationRegistration]:
        statement = self.select()
        if registration_type is not None:
            statement = statement.where(
                OrganizationRegistration.registration_type == registration_type
            )
        statement = statement.order_by(
            OrganizationRegistration.registration_type,
            OrganizationRegistration.registration_number,
        )
        return list(self._session.execute(statement).scalars())

    def find_duplicates(
        self,
        *,
        registration_type: str,
        registration_number: str,
        state_code: str | None,
        registration_label: str | None,
        system_wide: bool,
        state_unique: bool,
        label_unique: bool,
        exclude_id: uuid.UUID | None = None,
    ) -> Sequence[OrganizationRegistration]:
        """Rows that would collide with the §7.4 uniqueness scope.

        The database's generated ``unique_scope_key`` is authoritative; this query exists so
        a client receives ``409`` with a readable message instead of a translated constraint
        failure. It mirrors the generated-column encoding.
        """
        statement = select(OrganizationRegistration).where(
            OrganizationRegistration.registration_type == registration_type,
            OrganizationRegistration.registration_number == registration_number,
            OrganizationRegistration.archived_at.is_(None),
        )
        if not system_wide:
            statement = statement.where(
                OrganizationRegistration.organization_id == self.organization_id
            )
        if state_unique:
            statement = statement.where(OrganizationRegistration.state_code == state_code)
        if label_unique:
            statement = statement.where(
                OrganizationRegistration.registration_label == registration_label
            )
        if exclude_id is not None:
            statement = statement.where(OrganizationRegistration.id != exclude_id)
        return list(self._session.execute(statement).scalars())


class IdentifierPolicyRepository(OrgScopedRepository[IdentifierRequirementPolicy]):
    """Per-organization identifier collection requirements (§11.3)."""

    model = IdentifierRequirementPolicy

    def list_policies(self) -> Sequence[IdentifierRequirementPolicy]:
        statement = self.select().order_by(IdentifierRequirementPolicy.identifier_type)
        return list(self._session.execute(statement).scalars())

    def get_by_type(self, identifier_type: str) -> IdentifierRequirementPolicy | None:
        statement = self.select().where(
            IdentifierRequirementPolicy.identifier_type == identifier_type
        )
        return self._session.execute(statement).scalar_one_or_none()


class NominationConfigRepository(OrgScopedRepository[NominationTypeConfig]):
    """Per-organization nomination rules (§13.3)."""

    model = NominationTypeConfig

    def list_configs(self) -> Sequence[NominationTypeConfig]:
        statement = self.select().order_by(NominationTypeConfig.nomination_type)
        return list(self._session.execute(statement).scalars())

    def get_by_type(self, nomination_type: str) -> NominationTypeConfig | None:
        statement = self.select().where(NominationTypeConfig.nomination_type == nomination_type)
        return self._session.execute(statement).scalar_one_or_none()


__all__ = [
    "IdentifierPolicyRepository",
    "NominationConfigRepository",
    "OrganizationGroupRepository",
    "OrganizationRegistrationRepository",
    "OrganizationRepository",
    "OrganizationSettingsRepository",
]
