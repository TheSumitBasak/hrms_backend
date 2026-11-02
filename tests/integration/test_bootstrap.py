"""First-run bootstrap against a real database (§19 of the brief, §24.4.1).

Every test here starts from a schema that holds the platform seed and nothing else, because
the bootstrap's precondition *is* emptiness. The ``empty_platform`` fixture truncates and
re-seeds rather than relying on test order, so these tests neither depend on nor leave behind
rows other modules could see.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import Engine, func, inspect, select, text

from app.core.errors import ValidationFailed
from app.core.permissions import ALL_PERMISSIONS, ROLE_PERMISSIONS, Permission, Role
from app.models.access import OrganizationRolePermission, UserOrganizationAccess
from app.models.audit import AuditLog
from app.models.master import EmploymentType, LookupValue
from app.models.organization import Organization, OrganizationSettings
from app.models.platform import AuthProvider, User, UserStatus
from app.services.bootstrap import (
    BOOTSTRAP_ROLES,
    PLACEHOLDER_ADDRESS_TEXT,
    bootstrap_first_admin,
    bootstrap_skip_reason,
)
from tests.factories import create_organization, create_user

ADMIN_EMAIL = "admin@example.test"
ADMIN_PASSWORD = "bootstrap-admin-password"
ORGANIZATION_NAME = "test"

LOGIN = "/api/v1/auth/login"
ME = "/api/v1/auth/me"

# Held only by the SELF role (addendum §10.3) or the payroll roles, which the bootstrap
# administrator grants to people instead of holding (payroll addendum §16.1, maker-checker).
_PAYROLL_ROLES = (
    Role.PAYROLL_ADMINISTRATOR,
    Role.PAYROLL_PROCESSOR,
    Role.PAYROLL_APPROVER,
    Role.FINANCE_USER,
    Role.STATUTORY_COMPLIANCE_OFFICER,
    Role.PAYROLL_AUDITOR,
    Role.EMPLOYEE_ALUMNI,
)
OUTSIDE_BOOTSTRAP = {
    permission for permission in Permission if permission.value.startswith("self.")
} | (
    frozenset().union(*(ROLE_PERMISSIONS[role] for role in _PAYROLL_ROLES))
    - frozenset().union(*(ROLE_PERMISSIONS[role] for role in BOOTSTRAP_ROLES))
)


@pytest.fixture
def empty_platform(seeded_database: Engine) -> Iterator[Engine]:
    """A migrated, platform-seeded schema with no users and no organizations.

    Re-binds the process database handle every time, because a closed ``TestClient``
    lifespan disposes it (``app.main``) and the next ``get_session_factory()`` would
    otherwise build an engine from the ambient ``.env`` — the developer's own schema.
    """
    _reset_to_platform_seed(seeded_database)
    yield seeded_database
    # Leave the schema as the session fixture promises: seeded and otherwise empty.
    _reset_to_platform_seed(seeded_database)


def _reset_to_platform_seed(engine: Engine) -> None:
    """Truncate everything, then re-apply the platform seed."""
    from app.db.session import configure_engine, get_session_factory, unit_of_work
    from app.services.seed_data import seed_platform

    configure_engine(engine)
    _truncate_everything(engine)
    session = get_session_factory()()
    try:
        with unit_of_work(session):
            seed_platform(session)
    finally:
        session.close()


def _truncate_everything(engine: Engine) -> None:
    """Empty every table. Foreign-key checks are suspended for the §23.1 cycles."""
    with engine.begin() as connection:
        tables = [
            name for name in inspect(connection).get_table_names() if name != "alembic_version"
        ]
        connection.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
        for table in tables:
            connection.execute(text(f"TRUNCATE TABLE `{table}`"))
        connection.execute(text("SET FOREIGN_KEY_CHECKS = 1"))


@pytest.fixture
def bootstrap_session(empty_platform: Engine) -> Iterator[Any]:
    """A committing session on the empty schema, as the CLI uses."""
    from app.db.session import configure_engine, get_session_factory

    configure_engine(empty_platform)
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()


def _bootstrap(session: Any, **overrides: Any) -> Any:
    from app.db.session import unit_of_work

    arguments: dict[str, Any] = {
        "email": ADMIN_EMAIL,
        "password": ADMIN_PASSWORD,
        "organization_name": ORGANIZATION_NAME,
    }
    arguments.update(overrides)
    with unit_of_work(session):
        return bootstrap_first_admin(session, **arguments)


class TestBootstrapOnAnEmptyDatabase:
    def test_creates_one_organization_one_user_and_three_grants(
        self, bootstrap_session: Any
    ) -> None:
        report = _bootstrap(bootstrap_session)

        assert report.performed
        assert report.skip_reason is None
        assert report.organization_code == "TEST"
        assert report.email == ADMIN_EMAIL
        assert set(report.role_codes) == {role.value for role in BOOTSTRAP_ROLES}

        organization = bootstrap_session.get(Organization, report.organization_id)
        assert organization.display_name == ORGANIZATION_NAME
        assert organization.legal_name == ORGANIZATION_NAME
        assert organization.status == "ACTIVE"
        # §7.3: OTHER carries neither a CIN nor an LLPIN, which configuration cannot supply.
        assert organization.entity_type == "OTHER"
        assert organization.cin is None
        assert organization.llpin is None

        user = bootstrap_session.get(User, report.user_id)
        assert user.email == ADMIN_EMAIL
        assert user.status == UserStatus.ACTIVE.value
        assert user.auth_provider == AuthProvider.LOCAL.value
        assert user.password_hash and ADMIN_PASSWORD not in user.password_hash

        grants = (
            bootstrap_session.execute(
                select(UserOrganizationAccess).where(
                    UserOrganizationAccess.user_id == report.user_id
                )
            )
            .scalars()
            .all()
        )
        assert len(grants) == len(BOOTSTRAP_ROLES)
        assert all(grant.revoked_at is None for grant in grants)
        # Permanent, ordinary grants: §5.3's break-glass path is for emergencies, not setup.
        assert all(grant.expires_at is None for grant in grants)
        assert not any(grant.is_break_glass for grant in grants)

    def test_seeds_the_organization_exactly_as_api_creation_does(
        self, bootstrap_session: Any
    ) -> None:
        """§24.4.1: settings, lookups, categories, employment types and policies."""
        report = _bootstrap(bootstrap_session)
        organization_id = report.organization_id

        assert bootstrap_session.get(OrganizationSettings, organization_id) is not None
        for model in (LookupValue, EmploymentType):
            count = bootstrap_session.execute(
                select(func.count())
                .select_from(model)
                .where(model.organization_id == organization_id)
            ).scalar_one()
            assert count > 0, model.__name__

    def test_grants_every_permission_without_any_override_row(self, bootstrap_session: Any) -> None:
        """ "All access" must come from the §5.3 matrix, not from DEC-004 override rows."""
        from app.core.permissions import Role, resolve_permissions

        report = _bootstrap(bootstrap_session)

        overrides = bootstrap_session.execute(
            select(func.count()).select_from(OrganizationRolePermission)
        ).scalar_one()
        assert overrides == 0

        roles = frozenset(Role(code) for code in report.role_codes)
        assert resolve_permissions(roles).permissions == ALL_PERMISSIONS - OUTSIDE_BOOTSTRAP

    def test_records_the_platform_events(self, bootstrap_session: Any) -> None:
        """§20.2: creating an organization, a user and a grant are audited actions."""
        report = _bootstrap(bootstrap_session)
        actions = set(
            bootstrap_session.execute(
                select(AuditLog.action).where(AuditLog.actor_user_id == report.user_id)
            )
            .scalars()
            .all()
        )
        assert {"ORG_CREATED", "USER_CREATED", "ORG_ACCESS_GRANTED"} <= actions

    def test_the_registered_address_is_an_obvious_placeholder(self, bootstrap_session: Any) -> None:
        """§7.1's columns are NOT NULL; configuration supplies only a name (DEC note)."""
        report = _bootstrap(bootstrap_session)
        organization = bootstrap_session.get(Organization, report.organization_id)
        assert organization.registered_address_line_1 == PLACEHOLDER_ADDRESS_TEXT
        assert organization.registered_address_city == PLACEHOLDER_ADDRESS_TEXT
        assert report.address_is_placeholder

    def test_the_administrator_can_sign_in_and_holds_every_permission(
        self, bootstrap_session: Any, api_client: Any
    ) -> None:
        """End to end: the configured password works against the real login endpoint."""
        _bootstrap(bootstrap_session)

        login = api_client.post(LOGIN, json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
        assert login.status_code == 200, login.text

        me = api_client.get(ME, headers={"Authorization": f"Bearer {login.json()['access_token']}"})
        assert me.status_code == 200, me.text
        body = me.json()
        assert len(body["organizations"]) == 1
        organization = body["organizations"][0]
        assert organization["organization_code"] == "TEST"
        assert sorted(organization["roles"]) == sorted(role.value for role in BOOTSTRAP_ROLES)
        assert set(organization["permissions"]) == {
            permission.value for permission in ALL_PERMISSIONS - OUTSIDE_BOOTSTRAP
        }


class TestBootstrapIsAOneShot:
    def test_a_second_run_changes_nothing(self, bootstrap_session: Any) -> None:
        """Safe on every deployment: the second call must not re-provision anything."""
        first = _bootstrap(bootstrap_session)
        second = _bootstrap(bootstrap_session)

        assert second.performed is False
        assert second.skip_reason == "a user account already exists"
        users = bootstrap_session.execute(select(func.count()).select_from(User)).scalar_one()
        organizations = bootstrap_session.execute(
            select(func.count()).select_from(Organization)
        ).scalar_one()
        grants = bootstrap_session.execute(
            select(func.count()).select_from(UserOrganizationAccess)
        ).scalar_one()
        assert (users, organizations, grants) == (1, 1, len(BOOTSTRAP_ROLES))
        assert first.user_id == bootstrap_session.execute(select(User.id)).scalar_one()

    def test_skips_when_any_user_exists(self, bootstrap_session: Any) -> None:
        """An existing account means the platform is in use; never add another SSA."""
        create_user(bootstrap_session, email="someone@example.test")
        bootstrap_session.commit()

        report = _bootstrap(bootstrap_session)
        assert report.performed is False
        assert report.skip_reason == "a user account already exists"
        assert bootstrap_session.execute(select(func.count()).select_from(User)).scalar_one() == 1

    def test_skips_when_any_organization_exists(self, bootstrap_session: Any) -> None:
        """A tenant without users is still a configured platform, not a new database."""
        create_organization(bootstrap_session, seed=False)
        bootstrap_session.commit()

        report = _bootstrap(bootstrap_session)
        assert report.performed is False
        assert report.skip_reason == "an organization already exists"
        assert bootstrap_session.execute(select(func.count()).select_from(User)).scalar_one() == 0

    def test_skip_reason_is_none_on_an_empty_database(self, bootstrap_session: Any) -> None:
        assert bootstrap_skip_reason(bootstrap_session) is None


class TestBootstrapRejectsBadConfiguration:
    def test_a_short_password_writes_nothing(self, bootstrap_session: Any) -> None:
        """The documented ``Admin@12345`` is one character short of the §MIN policy."""
        with pytest.raises(ValidationFailed, match="SEED_ADMIN_PASSWORD"):
            _bootstrap(bootstrap_session, password="Admin@12345")
        bootstrap_session.rollback()
        assert bootstrap_session.execute(select(func.count()).select_from(User)).scalar_one() == 0
        assert (
            bootstrap_session.execute(select(func.count()).select_from(Organization)).scalar_one()
            == 0
        )

    def test_an_invalid_email_writes_nothing(self, bootstrap_session: Any) -> None:
        with pytest.raises(ValidationFailed, match="SEED_ADMIN_EMAIL"):
            _bootstrap(bootstrap_session, email="not-an-email")
        bootstrap_session.rollback()
        assert bootstrap_session.execute(select(func.count()).select_from(User)).scalar_one() == 0
