"""First-run bootstrap: the parts that need no database.

The role-coverage test is the important one. "A super admin with all access" is implemented
as three ordinary grants rather than a bypass, which is only true as long as those three roles
between them still cover every permission in the §5.3 matrix. Adding a permission to the enum
without placing it in one of them breaks that silently in production and loudly here.
"""

from __future__ import annotations

import re

import pytest

from app.core.errors import ValidationFailed
from app.core.permissions import (
    ALL_PERMISSIONS,
    ROLE_PERMISSIONS,
    Permission,
    Role,
    resolve_permissions,
)
from app.core.security import MAX_PASSWORD_LENGTH, MIN_PASSWORD_LENGTH
from app.services.bootstrap import (
    BOOTSTRAP_ROLES,
    MAX_ORGANIZATION_CODE_LENGTH,
    MAX_ORGANIZATION_NAME_LENGTH,
    derive_organization_code,
    validate_bootstrap_input,
)

CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9]{1,9}$")
"""§7.2 ``organization_code``, as the database CHECK constraint spells it."""


# Addendum §10.3: the SELF role's configurable self-service cells act on the caller's own
# employee record, which the bootstrap administrator does not have.
_OUTSIDE_BOOTSTRAP = frozenset(
    {
        Permission.SELF_ONBOARDING,
        Permission.SELF_ATTENDANCE,
        Permission.SELF_LEAVE,
    }
)


class TestBootstrapRoleCoverage:
    """What "all access" means."""

    def test_three_roles_cover_every_permission(self) -> None:
        """§5.3: SSA + OA + HRA is the whole matrix, so no override row is ever needed."""
        granted = resolve_permissions(frozenset(BOOTSTRAP_ROLES))
        assert granted.permissions == ALL_PERMISSIONS - _OUTSIDE_BOOTSTRAP

    def test_coverage_needs_no_configurable_cells(self) -> None:
        """The ``C`` cells (DEC-004) stay off: a bootstrap writes no override rows.

        Asserted separately from the union above because ``resolve_permissions`` would also
        pass if a ``C`` cell were required — this pins the base ``Y`` sets.
        """
        base: set[object] = set()
        for role in BOOTSTRAP_ROLES:
            base |= ROLE_PERMISSIONS[role]
        assert base == set(ALL_PERMISSIONS - _OUTSIDE_BOOTSTRAP)

    def test_system_super_admin_alone_is_not_enough(self) -> None:
        """§5.3 note 1: SSA holds no employee-data permission, hence the other two roles."""
        ssa_only = resolve_permissions(frozenset({Role.SYSTEM_SUPER_ADMIN}))
        assert ssa_only.permissions != ALL_PERMISSIONS


class TestOrganizationCodeDerivation:
    """§7.2: ``^[A-Z][A-Z0-9]{1,9}$``, derived from the configured name."""

    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("test", "TEST"),
            ("Acme Technologies Private Limited", "ACMETECHNO"),
            ("A-1 Traders", "A1TRADERS"),
            ("  spaced  out  ", "SPACEDOUT"),
            # Leading digit: the pattern requires a letter first.
            ("3M India", "ORG3MINDIA"),
            # No ASCII alphanumerics at all (a name in another script).
            ("संस्था", "ORG"),
            # One character would be a one-character code, which the pattern rejects.
            ("A", "A1"),
        ],
    )
    def test_derives_expected_code(self, name: str, expected: str) -> None:
        assert derive_organization_code(name) == expected

    @pytest.mark.parametrize(
        "name",
        [
            "test",
            "Acme Technologies Private Limited",
            "3M India",
            "संस्था",
            "A",
            "x" * MAX_ORGANIZATION_NAME_LENGTH,
            "9" * MAX_ORGANIZATION_NAME_LENGTH,
            "!!! ???",
        ],
    )
    def test_derived_code_satisfies_the_database_constraint(self, name: str) -> None:
        """Whatever the name, the derived code must be insertable."""
        code = derive_organization_code(name)
        assert CODE_PATTERN.match(code), code
        assert len(code) <= MAX_ORGANIZATION_CODE_LENGTH


class TestBootstrapInputValidation:
    """Configuration errors are reported against the environment variable at fault."""

    def test_normalises_email_and_name(self) -> None:
        configured = validate_bootstrap_input(
            email="  Admin@Example.COM ",
            password="x" * MIN_PASSWORD_LENGTH,
            organization_name="  test  ",
        )
        assert configured.email == "admin@example.com"
        assert configured.organization_name == "test"
        assert configured.organization_code == "TEST"

    @pytest.mark.parametrize("email", ["", "   ", "not-an-email", "admin@localhost", "a@b"])
    def test_rejects_an_unusable_email(self, email: str) -> None:
        with pytest.raises(ValidationFailed, match="SEED_ADMIN_EMAIL"):
            validate_bootstrap_input(
                email=email, password="x" * MIN_PASSWORD_LENGTH, organization_name="test"
            )

    @pytest.mark.parametrize(
        "password",
        [
            "",
            "Admin@12345",  # eleven characters: the documented example that is one short
            "x" * (MIN_PASSWORD_LENGTH - 1),
            "x" * (MAX_PASSWORD_LENGTH + 1),
        ],
    )
    def test_rejects_a_password_outside_the_policy(self, password: str) -> None:
        """A short password must be a configuration error, not a bcrypt traceback."""
        with pytest.raises(ValidationFailed, match="SEED_ADMIN_PASSWORD"):
            validate_bootstrap_input(
                email="admin@example.com", password=password, organization_name="test"
            )

    @pytest.mark.parametrize(
        "organization_name", ["", "   ", "A", "x" * (MAX_ORGANIZATION_NAME_LENGTH + 1)]
    )
    def test_rejects_an_unusable_organization_name(self, organization_name: str) -> None:
        with pytest.raises(ValidationFailed, match="ORGANIZATION_NAME"):
            validate_bootstrap_input(
                email="admin@example.com",
                password="x" * MIN_PASSWORD_LENGTH,
                organization_name=organization_name,
            )

    def test_accepts_the_longest_allowed_name(self) -> None:
        """The upper bound is inclusive: ``ck_organizations_display_name_length`` allows 100."""
        name = "N" * MAX_ORGANIZATION_NAME_LENGTH
        configured = validate_bootstrap_input(
            email="admin@example.com",
            password="x" * MIN_PASSWORD_LENGTH,
            organization_name=name,
        )
        assert configured.organization_name == name
