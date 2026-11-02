"""Data access for the platform tables: users, roles, grants, tokens, idempotency keys.

These are not organization-scoped, so they extend :class:`~app.repositories.base.Repository`
rather than the tenant-filtered base. ``user_organization_access`` is the exception that proves
the rule: it *carries* an ``organization_id``, but its whole purpose is to answer "which
organizations may this user touch?", so it cannot itself be filtered by an organization that
has not been established yet.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, select, update

from app.core.clock import utc_now
from app.core.crypto import hash_opaque_token
from app.core.permissions import Permission, Role
from app.models.access import OrganizationRolePermission, UserOrganizationAccess
from app.models.platform import (
    IdempotencyKey,
    LoginAttempt,
    RefreshToken,
    RolePermission,
    User,
    UserStatus,
)
from app.models.platform import Permission as PermissionRow
from app.models.platform import Role as RoleRow
from app.repositories.base import Repository


def _affected_rows(result: Any) -> int:
    """Rows affected by a DML statement.

    ``Session.execute`` is typed as returning ``Result``, but a DML statement actually
    returns a ``CursorResult`` carrying ``rowcount``. Narrowing it here keeps the one
    unavoidable cast in a single place rather than at four call sites.
    """
    return int(getattr(result, "rowcount", 0) or 0)


class UserRepository(Repository):
    """Reads and writes platform user accounts."""

    def get(self, user_id: uuid.UUID) -> User | None:
        return self._session.get(User, user_id)

    def get_by_email(self, email: str) -> User | None:
        """Look up by email.

        Emails are stored already lower-cased and trimmed (§21.1 V-EMAIL), so the caller must
        normalise before calling — otherwise a mixed-case login silently finds nothing.
        """
        statement = select(User).where(User.email == email)
        return self._session.execute(statement).scalar_one_or_none()

    def get_active_by_email(self, email: str) -> User | None:
        statement = select(User).where(
            User.email == email,
            User.status == UserStatus.ACTIVE,
            User.archived_at.is_(None),
        )
        return self._session.execute(statement).scalar_one_or_none()

    def get_active_for_employee(self, employee_id: uuid.UUID) -> User | None:
        """The active account linked to an employment record (approval routing, INT-31)."""
        statement = select(User).where(
            User.employee_id == employee_id,
            User.status == UserStatus.ACTIVE,
            User.archived_at.is_(None),
        )
        return self._session.execute(statement.limit(1)).scalar_one_or_none()

    def add(self, user: User) -> User:
        self._session.add(user)
        return user

    def list_for_organizations(
        self, organization_ids: Sequence[uuid.UUID], *, limit: int = 100
    ) -> Sequence[User]:
        """Users holding at least one active grant in the given organizations.

        §24.4.8: "OA sees only users with grants in their organizations". An Organization Admin
        must not be able to enumerate the platform's user list.
        """
        if not organization_ids:
            return []
        statement = (
            select(User)
            .join(UserOrganizationAccess, UserOrganizationAccess.user_id == User.id)
            .where(
                UserOrganizationAccess.organization_id.in_(organization_ids),
                UserOrganizationAccess.revoked_at.is_(None),
            )
            .distinct()
            .order_by(User.display_name, User.id)
            .limit(limit)
        )
        return list(self._session.execute(statement).scalars())

    def record_login(self, user: User, *, at: datetime | None = None) -> None:
        user.last_login_at = at or utc_now()


class RoleRepository(Repository):
    """Role definitions and their permission sets."""

    def get_by_code(self, role_code: str) -> RoleRow | None:
        statement = select(RoleRow).where(RoleRow.role_code == role_code)
        return self._session.execute(statement).scalar_one_or_none()

    def list_all(self) -> Sequence[RoleRow]:
        statement = select(RoleRow).order_by(RoleRow.role_code)
        return list(self._session.execute(statement).scalars())

    def permission_codes_for(self, role_id: uuid.UUID) -> frozenset[str]:
        statement = (
            select(PermissionRow.permission_code)
            .join(RolePermission, RolePermission.permission_id == PermissionRow.id)
            .where(RolePermission.role_id == role_id)
        )
        return frozenset(self._session.execute(statement).scalars())

    def replace_permissions(self, role: RoleRow, permission_ids: Sequence[uuid.UUID]) -> None:
        """Replace a role's permission set (§24.4.8 ``PUT /roles/{rid}/permissions``).

        A full replacement rather than a diff, because that is the contract the endpoint
        offers and because an incremental update would make "revoke this permission" an
        operation with no obvious call.
        """
        self._session.execute(delete(RolePermission).where(RolePermission.role_id == role.id))
        for permission_id in permission_ids:
            self._session.add(RolePermission(role_id=role.id, permission_id=permission_id))


class PermissionRepository(Repository):
    """The permission catalogue (§24.4.8 ``GET /permissions``)."""

    def list_all(self) -> Sequence[PermissionRow]:
        statement = select(PermissionRow).order_by(PermissionRow.permission_code)
        return list(self._session.execute(statement).scalars())

    def ids_for_codes(self, codes: Sequence[str]) -> list[uuid.UUID]:
        if not codes:
            return []
        statement = select(PermissionRow.id).where(PermissionRow.permission_code.in_(codes))
        return list(self._session.execute(statement).scalars())


class AccessGrantRepository(Repository):
    """Organization access grants — where authorization actually lives (§5.4)."""

    def active_grants_for_user(
        self, user_id: uuid.UUID, *, at: datetime | None = None
    ) -> Sequence[UserOrganizationAccess]:
        """Every grant the user currently holds, across all organizations.

        Filters out revoked and expired grants in SQL rather than in Python, so an expired
        break-glass grant cannot be included by a caller that forgets to check.
        """
        moment = at or utc_now()
        statement = (
            select(UserOrganizationAccess)
            .where(
                UserOrganizationAccess.user_id == user_id,
                UserOrganizationAccess.revoked_at.is_(None),
                UserOrganizationAccess.granted_at <= moment,
                (UserOrganizationAccess.expires_at.is_(None))
                | (UserOrganizationAccess.expires_at > moment),
            )
            .order_by(UserOrganizationAccess.organization_id)
        )
        return list(self._session.execute(statement).scalars())

    def active_grants_in_organization(
        self, user_id: uuid.UUID, organization_id: uuid.UUID, *, at: datetime | None = None
    ) -> Sequence[UserOrganizationAccess]:
        moment = at or utc_now()
        statement = select(UserOrganizationAccess).where(
            UserOrganizationAccess.user_id == user_id,
            UserOrganizationAccess.organization_id == organization_id,
            UserOrganizationAccess.revoked_at.is_(None),
            UserOrganizationAccess.granted_at <= moment,
            (UserOrganizationAccess.expires_at.is_(None))
            | (UserOrganizationAccess.expires_at > moment),
        )
        return list(self._session.execute(statement).scalars())

    def list_for_user(self, user_id: uuid.UUID) -> Sequence[UserOrganizationAccess]:
        """All grants including revoked ones (§23.2: "Revoked rows retained")."""
        statement = (
            select(UserOrganizationAccess)
            .where(UserOrganizationAccess.user_id == user_id)
            .order_by(UserOrganizationAccess.granted_at.desc())
        )
        return list(self._session.execute(statement).scalars())

    def get(self, grant_id: uuid.UUID) -> UserOrganizationAccess | None:
        return self._session.get(UserOrganizationAccess, grant_id)

    def add(self, grant: UserOrganizationAccess) -> UserOrganizationAccess:
        self._session.add(grant)
        return grant

    def revoke(
        self, grant: UserOrganizationAccess, *, by_user_id: uuid.UUID, reason: str | None = None
    ) -> None:
        """Soft-revoke. §23.2 retains the row so the audit trail stays complete."""
        grant.revoked_at = utc_now()
        grant.revoked_by = by_user_id
        if reason:
            grant.reason = reason

    def revoke_all_for_user(self, user_id: uuid.UUID, *, by_user_id: uuid.UUID) -> int:
        """Revoke every active grant for a user, for deactivation (§25.5)."""
        result = self._session.execute(
            update(UserOrganizationAccess)
            .where(
                UserOrganizationAccess.user_id == user_id,
                UserOrganizationAccess.revoked_at.is_(None),
            )
            .values(revoked_at=utc_now(), revoked_by=by_user_id)
        )
        return _affected_rows(result)


class OrganizationRolePermissionRepository(Repository):
    """The §5.3 ``C`` cells an organization has switched on (DEC-004)."""

    def enabled_for(self, organization_id: uuid.UUID, role_code: str) -> frozenset[Permission]:
        """Permissions enabled for one role in one organization.

        Rows naming a permission that is not a valid ``C`` cell for the role are ignored by
        :func:`app.core.permissions.resolve_permissions`, which intersects this against the
        role's declared configurable set — so a stale or tampered row cannot widen a role.
        """
        statement = select(OrganizationRolePermission.permission_code).where(
            OrganizationRolePermission.organization_id == organization_id,
            OrganizationRolePermission.role_code == role_code,
        )
        codes = self._session.execute(statement).scalars()
        resolved: set[Permission] = set()
        for code in codes:
            try:
                resolved.add(Permission(code))
            except ValueError:
                # An unknown code is configuration drift, not an authorization decision;
                # ignoring it fails closed.
                continue
        return frozenset(resolved)

    def enabled_for_roles(
        self, organization_id: uuid.UUID, role_codes: Sequence[str]
    ) -> dict[str, frozenset[Permission]]:
        """Enabled ``C`` cells for several roles in one query, avoiding an N+1 per request."""
        if not role_codes:
            return {}
        statement = select(
            OrganizationRolePermission.role_code, OrganizationRolePermission.permission_code
        ).where(
            OrganizationRolePermission.organization_id == organization_id,
            OrganizationRolePermission.role_code.in_(role_codes),
        )
        grouped: dict[str, set[Permission]] = {code: set() for code in role_codes}
        for role_code, permission_code in self._session.execute(statement):
            try:
                grouped[role_code].add(Permission(permission_code))
            except (ValueError, KeyError):
                continue
        return {code: frozenset(values) for code, values in grouped.items()}

    def set_enabled(
        self,
        *,
        organization_id: uuid.UUID,
        role_code: str,
        permission_code: str,
        enabled_by: uuid.UUID,
    ) -> OrganizationRolePermission:
        row = OrganizationRolePermission(
            id=uuid.uuid4(),
            organization_id=organization_id,
            role_code=role_code,
            permission_code=permission_code,
            enabled_by=enabled_by,
        )
        self._session.add(row)
        return row

    def clear(self, *, organization_id: uuid.UUID, role_code: str, permission_code: str) -> None:
        self._session.execute(
            delete(OrganizationRolePermission).where(
                OrganizationRolePermission.organization_id == organization_id,
                OrganizationRolePermission.role_code == role_code,
                OrganizationRolePermission.permission_code == permission_code,
            )
        )


class RefreshTokenRepository(Repository):
    """Refresh token lifecycle (DEC-005, §25.5).

    Tokens are matched by digest; the plaintext is never stored, so a database disclosure
    yields nothing usable.
    """

    def issue(
        self,
        *,
        user_id: uuid.UUID,
        token: str,
        ttl: timedelta,
        user_agent: str | None = None,
        ip_address: str | None = None,
    ) -> RefreshToken:
        now = utc_now()
        row = RefreshToken(
            id=uuid.uuid4(),
            user_id=user_id,
            token_hash=hash_opaque_token(token),
            expires_at=now + ttl,
            user_agent=user_agent,
            ip_address=ip_address,
        )
        self._session.add(row)
        return row

    def find_usable(self, token: str, *, at: datetime | None = None) -> RefreshToken | None:
        """Find an unrevoked, unexpired token by its digest."""
        moment = at or utc_now()
        statement = select(RefreshToken).where(
            RefreshToken.token_hash == hash_opaque_token(token),
            RefreshToken.revoked_at.is_(None),
            RefreshToken.expires_at > moment,
        )
        return self._session.execute(statement).scalar_one_or_none()

    def find_any(self, token: str) -> RefreshToken | None:
        """Find a token regardless of state, so a replayed revoked token is detectable."""
        statement = select(RefreshToken).where(RefreshToken.token_hash == hash_opaque_token(token))
        return self._session.execute(statement).scalar_one_or_none()

    def revoke(self, row: RefreshToken, *, reason: str) -> None:
        row.revoked_at = utc_now()
        row.revoked_reason = reason

    def rotate(
        self,
        old: RefreshToken,
        *,
        token: str,
        ttl: timedelta,
        user_agent: str | None = None,
        ip_address: str | None = None,
    ) -> RefreshToken:
        """Revoke ``old`` and issue its successor, linking the two.

        Linking makes a replay of the superseded token detectable rather than merely invalid,
        which is the signal that a refresh token has been stolen.
        """
        replacement = self.issue(
            user_id=old.user_id,
            token=token,
            ttl=ttl,
            user_agent=user_agent,
            ip_address=ip_address,
        )
        self._session.flush()
        self.revoke(old, reason="ROTATED")
        old.replaced_by_id = replacement.id
        return replacement

    def revoke_all_for_user(self, user_id: uuid.UUID, *, reason: str) -> int:
        """Revoke every live token for a user.

        §25.5: "user deactivation revokes refresh tokens immediately". One indexed UPDATE, so
        revocation does not depend on token format or on waiting for expiry.
        """
        result = self._session.execute(
            update(RefreshToken)
            .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=utc_now(), revoked_reason=reason)
        )
        return _affected_rows(result)

    def purge_expired(self, *, before: datetime | None = None) -> int:
        """Delete tokens that expired before ``before``. Housekeeping only."""
        cutoff = before or utc_now()
        result = self._session.execute(delete(RefreshToken).where(RefreshToken.expires_at < cutoff))
        return _affected_rows(result)


class LoginAttemptRepository(Repository):
    """Append-only authentication attempts, for rate limiting and lockout (§25.9)."""

    def record(
        self,
        *,
        email_hash: str,
        succeeded: bool,
        user_id: uuid.UUID | None = None,
        failure_reason: str | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> LoginAttempt:
        row = LoginAttempt(
            id=uuid.uuid4(),
            email_hash=email_hash,
            user_id=user_id,
            succeeded=succeeded,
            failure_reason=failure_reason,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        self._session.add(row)
        return row

    def recent_failure_count(self, *, email_hash: str, since: datetime) -> int:
        statement = (
            select(LoginAttempt.id)
            .where(
                LoginAttempt.email_hash == email_hash,
                LoginAttempt.succeeded.is_(False),
                LoginAttempt.created_at >= since,
            )
            .limit(1000)
        )
        return len(list(self._session.execute(statement).scalars()))


class IdempotencyRepository(Repository):
    """Stored idempotent requests (DEC-006, §24.1, §24.5)."""

    def find(self, *, user_id: uuid.UUID, key: uuid.UUID) -> IdempotencyKey | None:
        statement = select(IdempotencyKey).where(
            IdempotencyKey.user_id == user_id, IdempotencyKey.idempotency_key == key
        )
        return self._session.execute(statement).scalar_one_or_none()

    def begin(
        self,
        *,
        user_id: uuid.UUID,
        key: uuid.UUID,
        method: str,
        path: str,
        fingerprint: str,
        ttl: timedelta,
    ) -> IdempotencyKey:
        """Claim a key before the operation runs.

        Inserted first so a concurrent duplicate collides on the unique constraint rather than
        both requests proceeding — which for employee creation would consume two sequence
        values (E-03).
        """
        row = IdempotencyKey(
            id=uuid.uuid4(),
            user_id=user_id,
            idempotency_key=key,
            request_method=method,
            request_path=path,
            request_fingerprint=fingerprint,
            expires_at=utc_now() + ttl,
        )
        self._session.add(row)
        return row

    def complete(self, row: IdempotencyKey, *, status_code: int, body: str) -> None:
        """Store the response so a replay returns it verbatim."""
        row.response_status = status_code
        row.response_body = body
        row.completed_at = utc_now()

    def purge_expired(self, *, before: datetime | None = None) -> int:
        cutoff = before or utc_now()
        result = self._session.execute(
            delete(IdempotencyKey).where(IdempotencyKey.expires_at < cutoff)
        )
        return _affected_rows(result)


SYSTEM_ROLE_DEFINITIONS: dict[Role, tuple[str, str]] = {
    Role.SYSTEM_SUPER_ADMIN: (
        "System Super Admin",
        "Creates organizations and groups, manages platform users and role definitions. "
        "No employee data access by default.",
    ),
    Role.ORGANIZATION_ADMIN: (
        "Organization Admin",
        "Configures organization profile, settings, masters, templates and users. "
        "Non-sensitive employee read access.",
    ),
    Role.HR_ADMIN: (
        "HR Admin",
        "Full HR data authority including sensitive fields, salary revisions and corrections.",
    ),
    Role.HR_EXECUTIVE: (
        "HR Executive",
        "Day-to-day data entry. No reveal of full identifiers or bank numbers.",
    ),
    Role.DEPARTMENT_MANAGER: (
        "Department Manager",
        "Read-only non-sensitive work profile of scoped employees.",
    ),
    Role.READ_ONLY_VIEWER: (
        "Read-only Viewer",
        "Read-only non-sensitive work profile of all employees in the organization.",
    ),
    Role.RESTRICTED_DOCUMENT_REVIEWER: (
        "Restricted Document Reviewer",
        "Minimal profile plus documents including restricted categories; masked identifiers.",
    ),
    Role.EMPLOYEE_SELF: (
        "Employee (self)",
        "Optional self-view of the caller's own employment record.",
    ),
    Role.ONBOARDING_TASK_OWNER: (
        "Onboarding Task Owner",
        "Executes or reviews onboarding tasks assigned to the user or the user's queue. "
        "Minimal profile of those tasks' employees only.",
    ),
    Role.ATTENDANCE_INTEGRATION_CLIENT: (
        "Attendance Integration Client",
        "Service account that imports punches. No read access beyond import results.",
    ),
    Role.PAYROLL_ADMINISTRATOR: (
        "Payroll Administrator",
        "Payroll configuration, pay groups, component behaviour, payslip templates and reports.",
    ),
    Role.PAYROLL_PROCESSOR: (
        "Payroll Processor",
        "Inputs, run creation, calculation, submission, payslip operations and F&F preparation.",
    ),
    Role.PAYROLL_APPROVER: (
        "Payroll Approver",
        "Reviews, approves, locks and reopens payroll runs and approves F&F.",
    ),
    Role.FINANCE_USER: (
        "Finance User",
        "Payments, bank advice files, loan disbursement and challan payment recording.",
    ),
    Role.STATUTORY_COMPLIANCE_OFFICER: (
        "Statutory Compliance Officer",
        "Statutory registration settings, challans, returns, reconciliation and certificates.",
    ),
    Role.PAYROLL_AUDITOR: (
        "Payroll Auditor",
        "Read-only access to payroll, tax, statutory and audit data. No reveal.",
    ),
    Role.EMPLOYEE_ALUMNI: (
        "Employee alumni",
        "Restricted self-service access to the employee's own payslips and tax documents.",
    ),
}
"""§5.1 role names and purposes, used by the seeder."""


__all__ = [
    "SYSTEM_ROLE_DEFINITIONS",
    "AccessGrantRepository",
    "IdempotencyRepository",
    "LoginAttemptRepository",
    "OrganizationRolePermissionRepository",
    "PermissionRepository",
    "RefreshTokenRepository",
    "RoleRepository",
    "UserRepository",
]
