"""First-run bootstrap: the initial organization and its administrator.

:func:`seed_platform` deliberately creates no user (§19 of the brief forbids shipping a
privileged account with a known password), so a freshly migrated schema has no way in. This
module is that explicit, separate operator action: on a database that holds **no users and no
organizations**, it creates one organization and one local administrator from configuration —
``ORGANIZATION_NAME``, ``SEED_ADMIN_EMAIL`` and ``SEED_ADMIN_PASSWORD``.

Two properties matter:

**It is a one-shot.** The precondition is emptiness, not "no user with this email". Once any
user or any organization exists the call is a no-op, so running it on every deployment can
never re-provision an administrator, resurrect a deleted account or widen an existing one.

**It grants through the ordinary mechanism.** The administrator receives three real
``user_organization_access`` rows — SSA, OA and HRA — rather than a bypass flag. The §5.3
matrix gives that union every permission in :data:`app.core.permissions.ALL_PERMISSIONS`
without a single per-organization override, so "all access" needs no special case anywhere in
the permission resolver, and revoking one of the three narrows the account exactly as it would
any other user. SSA alone would not do: §5.3 note 1 gives it no employee-data permission at
all.

The organization's registered address is a **placeholder** (§7.1 makes those columns
mandatory and the configuration supplies only a name). The report says so, and the operator
is expected to correct it through ``PATCH /organizations/{id}`` before real data is entered.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.actions import AuditAction
from app.audit.events import AuditEvent, build_change_set
from app.audit.writer import AuditWriter
from app.core.clock import utc_now
from app.core.errors import PreconditionFailed, ValidationFailed
from app.core.permissions import Role
from app.core.security import MAX_PASSWORD_LENGTH, MIN_PASSWORD_LENGTH, hash_password
from app.db.types import uuid7
from app.models.access import UserOrganizationAccess
from app.models.organization import EntityType, Organization
from app.models.platform import AuthProvider, User, UserStatus
from app.models.platform import Role as RoleRow
from app.services.seed_data import seed_organization
from app.validation.normalizers import normalize_email, normalize_text
from app.validation.rules import RuleId, check

BOOTSTRAP_ROLES: tuple[Role, ...] = (
    Role.SYSTEM_SUPER_ADMIN,
    Role.ORGANIZATION_ADMIN,
    Role.HR_ADMIN,
    # Sole holder of ATTENDANCE_IMPORT; without it the "all access" admin cannot import punches.
    Role.ATTENDANCE_INTEGRATION_CLIENT,
)
"""The roles whose union is every permission in the §5.3 matrix except the ``SELF_*`` cells.

Asserted by ``tests/unit/test_bootstrap.py``: if a future permission is added to the enum
without landing in one of these roles, that test fails rather than this module quietly
handing out an administrator who cannot administer."""

MIN_ORGANIZATION_NAME_LENGTH = 2
MAX_ORGANIZATION_NAME_LENGTH = 100
"""``ck_organizations_display_name_length``. ``legal_name`` allows 200, so the narrower of the
two bounds the single configured name."""

MAX_ORGANIZATION_CODE_LENGTH = 10
CODE_FALLBACK_PREFIX = "ORG"
"""§7.2 requires ``^[A-Z][A-Z0-9]{1,9}$``, so a name that starts with a digit or contains no
ASCII alphanumerics at all (a name written in a non-Latin script, for example) needs a prefix
rather than a rejection."""

PLACEHOLDER_ADDRESS_TEXT = "To be updated"
PLACEHOLDER_POSTAL_CODE = "000000"
"""§7.1's registered-address columns are NOT NULL and configuration supplies only a name.
Deliberately obvious placeholders: an operator who leaves them in place can see that they are
unset, which a plausible-looking fabricated address would hide."""

BOOTSTRAP_ACTOR_ROLE_CODES = Role.SYSTEM_SUPER_ADMIN.value
"""§20.1 ``actor_role_codes``. The bootstrap administrator acts as an SSA creating itself; the
grant rows are written in the same transaction, so the recorded role is the one it holds the
moment the transaction commits."""


@dataclass(frozen=True, slots=True)
class BootstrapReport:
    """What the bootstrap did, or why it declined to act.

    ``performed`` is false whenever ``skip_reason`` is set, and the two are the only states:
    a partial bootstrap is impossible because the whole thing runs in one transaction.
    """

    performed: bool
    skip_reason: str | None = None
    organization_id: uuid.UUID | None = None
    organization_code: str | None = None
    organization_name: str | None = None
    user_id: uuid.UUID | None = None
    email: str | None = None
    role_codes: tuple[str, ...] = ()
    address_is_placeholder: bool = False


def bootstrap_skip_reason(session: Session) -> str | None:
    """Why the bootstrap should not run, or ``None`` when the database is empty.

    Checked as two ``LIMIT 1`` existence probes rather than counts: the answer is "is there
    anything at all", and on a populated platform a count would scan rows to reach a number
    nobody reads.
    """
    if session.execute(select(User.id).limit(1)).first() is not None:
        return "a user account already exists"
    if session.execute(select(Organization.id).limit(1)).first() is not None:
        return "an organization already exists"
    return None


def derive_organization_code(organization_name: str) -> str:
    """Derive a §7.2-valid ``organization_code`` from the configured name.

    ``^[A-Z][A-Z0-9]{1,9}$``: ASCII alphanumerics only, first character a letter, two to ten
    characters. Uniqueness needs no suffix search because the bootstrap only runs when no
    organization exists.
    """
    cleaned = "".join(
        character
        for character in organization_name.upper()
        if character.isascii() and character.isalnum()
    )
    if not cleaned or not cleaned[0].isalpha():
        cleaned = f"{CODE_FALLBACK_PREFIX}{cleaned}"
    code = cleaned[:MAX_ORGANIZATION_CODE_LENGTH]
    # A single-character name ("A") yields a one-character code, which the pattern rejects.
    return code if len(code) >= 2 else f"{code}1"


@dataclass(frozen=True, slots=True)
class BootstrapInput:
    """The configured values, normalised and checked against the §7.2/§21.1 rules."""

    email: str
    organization_name: str
    organization_code: str


def validate_bootstrap_input(
    *, email: str, password: str, organization_name: str
) -> BootstrapInput:
    """Check the three configured values and return the normalised form.

    ``password`` is validated but not returned: it is used exactly as configured, and a
    second copy of it in a value object would be one more place for it to be logged.

    Raises :class:`ValidationFailed` with a message naming the environment variable at fault,
    which is the only thing an operator can act on.
    """
    normalized_email = _validated_email(email)
    _validate_password(password)
    organization = _validated_organization_name(organization_name)
    return BootstrapInput(
        email=normalized_email,
        organization_name=organization,
        organization_code=derive_organization_code(organization),
    )


def bootstrap_first_admin(
    session: Session,
    *,
    email: str,
    password: str,
    organization_name: str,
) -> BootstrapReport:
    """Create the first organization and its administrator, if the database is empty.

    Caller supplies the transaction (``unit_of_work``), so the organization, its §24.4.1 seed
    data, the user, the three grants and the audit rows commit together or not at all.

    Raises :class:`ValidationFailed` on unusable configuration and
    :class:`PreconditionFailed` when the platform roles have not been seeded yet.
    """
    if (reason := bootstrap_skip_reason(session)) is not None:
        return BootstrapReport(performed=False, skip_reason=reason)

    configured = validate_bootstrap_input(
        email=email, password=password, organization_name=organization_name
    )
    normalized_email = configured.email
    display_name = configured.organization_name
    organization_code = configured.organization_code
    roles = _required_roles(session)

    audit = AuditWriter(session)
    granted_at = utc_now()

    # The user is created first so every row it then creates can carry a real ``created_by``;
    # §1.5 leaves that column nullable only for the seed process, which this is not.
    user = User(
        id=uuid7(),
        email=normalized_email,
        display_name="Super Admin",
        auth_provider=AuthProvider.LOCAL.value,
        password_hash=hash_password(password),
        mfa_enabled=False,
        status=UserStatus.ACTIVE.value,
    )
    session.add(user)
    session.flush()

    organization = Organization(
        id=uuid7(),
        organization_code=organization_code,
        legal_name=display_name,
        display_name=display_name,
        # §7.3: OTHER is the one entity type that requires neither a CIN nor an LLPIN, which
        # configuration cannot supply. The operator sets the real type afterwards.
        entity_type=EntityType.OTHER.value,
        status="ACTIVE",
        registered_address_line_1=PLACEHOLDER_ADDRESS_TEXT,
        registered_address_city=PLACEHOLDER_ADDRESS_TEXT,
        registered_address_state=PLACEHOLDER_ADDRESS_TEXT,
        registered_address_country="IN",
        registered_address_postal_code=PLACEHOLDER_POSTAL_CODE,
        # financial_year_start_month, default_currency and timezone take their model
        # defaults (April, INR, Asia/Kolkata), which are the §7.5 defaults.
        created_by=user.id,
    )
    session.add(organization)
    session.flush()

    # Exactly what organization creation does through the API (§24.4.1), so a bootstrapped
    # organization is configured identically to one created later by an SSA.
    seed_organization(session, organization.id)

    for role in BOOTSTRAP_ROLES:
        grant = UserOrganizationAccess(
            id=uuid7(),
            user_id=user.id,
            organization_id=organization.id,
            role_id=roles[role],
            granted_by=user.id,
            granted_at=granted_at,
            expires_at=None,
            reason=None,
            is_break_glass=False,
        )
        session.add(grant)
        session.flush()
        audit.record(
            AuditEvent(
                action=AuditAction.ORG_ACCESS_GRANTED,
                entity_type="user_organization_access",
                entity_id=grant.id,
                actor_user_id=user.id,
                actor_role_codes=BOOTSTRAP_ACTOR_ROLE_CODES,
                organization_id=organization.id,
                changes=build_change_set(None, {"role_code": role.value}),
            )
        )

    audit.record(
        AuditEvent(
            action=AuditAction.USER_CREATED,
            entity_type="user",
            entity_id=user.id,
            actor_user_id=user.id,
            actor_role_codes=BOOTSTRAP_ACTOR_ROLE_CODES,
            # §20.1: user creation belongs to no single organization.
            organization_id=None,
            changes=build_change_set(
                None,
                {
                    "email": user.email,
                    "display_name": user.display_name,
                    "auth_provider": user.auth_provider,
                    "mfa_enabled": user.mfa_enabled,
                },
            ),
        )
    )
    audit.record(
        AuditEvent(
            action=AuditAction.ORG_CREATED,
            entity_type="organization",
            entity_id=organization.id,
            actor_user_id=user.id,
            actor_role_codes=BOOTSTRAP_ACTOR_ROLE_CODES,
            organization_id=organization.id,
            changes=build_change_set(
                None,
                {
                    "organization_code": organization.organization_code,
                    "legal_name": organization.legal_name,
                    "display_name": organization.display_name,
                    "entity_type": organization.entity_type,
                    "status": organization.status,
                },
            ),
        )
    )

    return BootstrapReport(
        performed=True,
        organization_id=organization.id,
        organization_code=organization.organization_code,
        organization_name=display_name,
        user_id=user.id,
        email=user.email,
        role_codes=tuple(role.value for role in BOOTSTRAP_ROLES),
        address_is_placeholder=True,
    )


def _validated_email(email: str) -> str:
    """Normalise and validate the configured administrator address (§21.1 V-EMAIL)."""
    normalized = normalize_email(email)
    if not normalized:
        raise ValidationFailed("SEED_ADMIN_EMAIL is required to bootstrap an administrator.")
    outcome = check(RuleId.EMAIL, normalized)
    if not outcome.ok:
        raise ValidationFailed(f"SEED_ADMIN_EMAIL is not a valid email address: {outcome.message}")
    return outcome.normalized or normalized


def _validate_password(password: str) -> None:
    """Apply the stored-password policy before bcrypt does.

    :func:`hash_password` raises :class:`ValueError` on a short password, which would surface
    to an operator as a traceback. The same limit stated as a configuration error is the
    difference between "fix SEED_ADMIN_PASSWORD" and "read the stack trace".
    """
    if not MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH:
        raise ValidationFailed(
            f"SEED_ADMIN_PASSWORD must be between {MIN_PASSWORD_LENGTH} and "
            f"{MAX_PASSWORD_LENGTH} characters."
        )


def _validated_organization_name(organization_name: str) -> str:
    """Trim the configured organization name and check it against the §7.2 length bounds."""
    normalized = normalize_text(organization_name)
    if not normalized:
        raise ValidationFailed("ORGANIZATION_NAME is required to bootstrap an organization.")
    if not MIN_ORGANIZATION_NAME_LENGTH <= len(normalized) <= MAX_ORGANIZATION_NAME_LENGTH:
        raise ValidationFailed(
            f"ORGANIZATION_NAME must be between {MIN_ORGANIZATION_NAME_LENGTH} and "
            f"{MAX_ORGANIZATION_NAME_LENGTH} characters."
        )
    return normalized


def _required_roles(session: Session) -> dict[Role, uuid.UUID]:
    """Resolve the three bootstrap roles to their seeded row ids.

    A missing role means the platform seed has not run. Failing with that sentence beats
    creating an administrator with no grants, which would look like a successful bootstrap
    and behave like a locked door.
    """
    rows = {
        row.role_code: row.id
        for row in session.execute(
            select(RoleRow.role_code, RoleRow.id).where(
                RoleRow.role_code.in_([role.value for role in BOOTSTRAP_ROLES])
            )
        ).all()
    }
    missing = [role.value for role in BOOTSTRAP_ROLES if role.value not in rows]
    if missing:
        raise PreconditionFailed(
            "The platform roles are not seeded "
            f"({', '.join(missing)} missing); run 'python -m app.cli seed' first."
        )
    return {role: rows[role.value] for role in BOOTSTRAP_ROLES}


__all__ = [
    "BOOTSTRAP_ROLES",
    "BootstrapInput",
    "BootstrapReport",
    "bootstrap_first_admin",
    "bootstrap_skip_reason",
    "derive_organization_code",
    "validate_bootstrap_input",
]
