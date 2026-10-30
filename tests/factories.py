"""Test data builders.

Kept out of ``conftest.py`` so they can be imported explicitly by the tests that use them
rather than arriving as ambient fixtures, and so the shape of the data under test is visible
at the call site.

**No realistic personal data.** §9 of the brief and §25.2 rule out real-looking sensitive
values in fixtures, and §1.4 states that all identifier examples are fictitious. Where a
checksum-bearing identifier is needed it is generated from the algorithm rather than written
as a literal, so no string resembling a real Aadhaar or GSTIN appears in the repository.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.core.clock import utc_now
from app.core.permissions import Role
from app.core.security import hash_password
from app.db.types import uuid7
from app.models.access import OrganizationRolePermission, UserOrganizationAccess
from app.models.organization import Organization, OrganizationGroup
from app.models.platform import AuthProvider, User, UserStatus
from app.models.platform import Role as RoleRow
from app.services.seed_data import seed_organization

DEFAULT_PASSWORD = "correct-horse-battery-staple"
"""Long enough to satisfy the §MIN_PASSWORD_LENGTH policy. Used only by the test suite."""


def unique_suffix() -> str:
    """A short unique token, so parallel or repeated runs do not collide on unique keys."""
    return uuid.uuid4().hex[:8]


@dataclass(slots=True)
class OrganizationFixture:
    """An organization and the ids a test needs from it."""

    id: uuid.UUID
    code: str
    display_name: str


@dataclass(slots=True)
class UserFixture:
    """A user account plus the credentials and grants a test gave it."""

    id: uuid.UUID
    email: str
    password: str = DEFAULT_PASSWORD
    grants: list[tuple[uuid.UUID, Role]] = field(default_factory=list)


def create_organization_group(session: Session, *, code: str | None = None) -> uuid.UUID:
    """A group, for the §11.5 group-scoped duplicate-detection tests."""
    group = OrganizationGroup(
        id=uuid7(),
        group_code=(code or f"G{unique_suffix()[:6]}").upper(),
        group_name=f"Group {unique_suffix()}",
        status="ACTIVE",
    )
    session.add(group)
    session.flush()
    return group.id


def create_organization(
    session: Session,
    *,
    code: str | None = None,
    group_id: uuid.UUID | None = None,
    entity_type: str = "PRIVATE_LIMITED",
    seed: bool = True,
) -> OrganizationFixture:
    """Create an organization, optionally with its §24.4.1 seed data.

    ``seed=True`` mirrors what organization creation does in production: settings, lookups,
    document categories, employment types, identifier policies and nomination configs, all in
    the same transaction. Tests that only need a tenant boundary can pass ``seed=False`` and
    skip roughly a hundred inserts.
    """
    suffix = unique_suffix()
    organization_code = (code or f"T{suffix[:5]}").upper()
    organization = Organization(
        id=uuid7(),
        organization_code=organization_code,
        group_id=group_id,
        legal_name=f"{organization_code} Technologies Private Limited",
        display_name=f"{organization_code} Tech",
        entity_type=entity_type,
        # §7.3: a private limited company requires a CIN; other types must not have one.
        cin=f"U72200GJ2015PTC{suffix[:6].upper()}" if entity_type == "PRIVATE_LIMITED" else None,
        registered_address_line_1="Plot 12, GIDC Electronic Estate",
        registered_address_city="Gandhinagar",
        registered_address_state="Gujarat",
        registered_address_state_code="24",
        registered_address_country="IN",
        registered_address_postal_code="382025",
        financial_year_start_month=4,
        default_currency="INR",
        timezone="Asia/Kolkata",
        status="ACTIVE",
    )
    session.add(organization)
    session.flush()
    if seed:
        seed_organization(session, organization.id)
        session.flush()
    return OrganizationFixture(
        id=organization.id,
        code=organization_code,
        display_name=organization.display_name,
    )


def create_user(
    session: Session,
    *,
    email: str | None = None,
    password: str | None = DEFAULT_PASSWORD,
    display_name: str = "Test User",
    grants: list[tuple[uuid.UUID, Role]] | None = None,
    employee_id: uuid.UUID | None = None,
    status: str = UserStatus.ACTIVE.value,
    auth_provider: str = AuthProvider.LOCAL.value,
    expires_at: datetime | None = None,
    is_break_glass: bool = False,
) -> UserFixture:
    """Create a user with optional organization grants.

    ``grants`` is a list of ``(organization_id, role)``. Each becomes a real
    ``user_organization_access`` row, so the test exercises the same resolution path a request
    does rather than a shortcut.
    """
    address = email or f"user-{unique_suffix()}@example.test"
    user = User(
        id=uuid7(),
        email=address.lower(),
        display_name=display_name,
        employee_id=employee_id,
        auth_provider=auth_provider,
        password_hash=(
            hash_password(password)
            if password and auth_provider == AuthProvider.LOCAL.value
            else None
        ),
        mfa_enabled=False,
        status=status,
    )
    session.add(user)
    session.flush()

    # `expires_at > granted_at` is a database CHECK, so an already-expired grant must be
    # backdated rather than issued from now. Needed to build the expired-grant fixtures
    # the §5.3 break-glass tests rely on.
    granted_at = utc_now() - timedelta(seconds=1)
    if expires_at is not None and expires_at <= granted_at:
        granted_at = expires_at - timedelta(minutes=1)

    assigned: list[tuple[uuid.UUID, Role]] = []
    for organization_id, role in grants or []:
        role_row = session.query(RoleRow).filter(RoleRow.role_code == role.value).one_or_none()
        if role_row is None:
            raise AssertionError(
                f"role {role.value} is not seeded; use the seeded_database fixture"
            )
        session.add(
            UserOrganizationAccess(
                id=uuid7(),
                user_id=user.id,
                organization_id=organization_id,
                role_id=role_row.id,
                granted_by=None,
                granted_at=granted_at,
                expires_at=expires_at,
                reason="Granted by the test suite" if is_break_glass else None,
                is_break_glass=is_break_glass,
            )
        )
        assigned.append((organization_id, role))
    session.flush()
    return UserFixture(
        id=user.id,
        email=user.email,
        password=password or "",
        grants=assigned,
    )


def enable_configurable_permission(
    session: Session,
    *,
    organization_id: uuid.UUID,
    role: Role,
    permission_code: str,
) -> None:
    """Switch on one §5.3 ``C`` cell for an organization (DEC-004)."""
    session.add(
        OrganizationRolePermission(
            id=uuid7(),
            organization_id=organization_id,
            role_code=role.value,
            permission_code=permission_code,
            enabled_by=None,
        )
    )
    session.flush()


__all__ = [
    "DEFAULT_PASSWORD",
    "OrganizationFixture",
    "UserFixture",
    "create_organization",
    "create_organization_group",
    "create_user",
    "enable_configurable_permission",
    "unique_suffix",
]
