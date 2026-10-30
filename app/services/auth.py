"""Authentication: local login, refresh rotation, logout (§24.1, §25.5, NFR-13).

Local password accounts are the documented fallback when no identity provider is configured
(A-04). Where OIDC is configured, a token is verified by
:class:`app.core.security.OidcTokenVerifier` and never reaches this service — but the user
record, the grants and the audit trail are the same either way.

Security properties this service is responsible for:

* **Uniform failure.** A wrong password, an unknown email, an inactive account and a
  non-local account all produce the same ``401 UNAUTHENTICATED`` with the same message, and
  the password comparison runs even when there is no user, so neither the response nor the
  timing distinguishes them (OWASP A07 user enumeration).
* **Refresh rotation with replay detection.** Each refresh consumes its token and issues a
  successor. Presenting an already-rotated token revokes the whole family, because the only
  way that happens is if a token was captured.
* **Immediate revocation.** Logout revokes the presented token; deactivation revokes every
  token for the user (§25.5), handled by :class:`app.services.access.AccessService`.
* **Rate limiting.** Repeated failures for one email are counted and blocked (§25.9).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy.orm import Session

from app.audit.actions import AuditAction
from app.audit.events import AuditEvent
from app.audit.writer import AuditWriter
from app.core.clock import utc_now
from app.core.context import RequestMetadata
from app.core.crypto import generate_opaque_token
from app.core.errors import RateLimited, Unauthenticated
from app.core.security import LocalTokenIssuer, hash_password, verify_password
from app.models.platform import AuthProvider, User
from app.repositories.platform import (
    LoginAttemptRepository,
    RefreshTokenRepository,
    UserRepository,
)
from app.validation.normalizers import normalize_email

MAX_FAILED_ATTEMPTS_PER_WINDOW = 10
FAILED_ATTEMPT_WINDOW = timedelta(minutes=15)
"""§25.9 asks for rate limiting on authentication. Ten failures for one address in fifteen
minutes is the threshold; the value is deliberately generous, because the goal is to stop
credential stuffing rather than to lock out someone mistyping a password."""

GENERIC_LOGIN_FAILURE = "The email address or password is incorrect."
"""One message for every failure mode. A more helpful message would be an enumeration oracle."""


@dataclass(frozen=True, slots=True)
class TokenPair:
    """What a successful authentication returns."""

    access_token: str
    refresh_token: str
    expires_in_seconds: int
    # S105 flags this only because the field name contains "token"; "Bearer" is the
    # RFC 6750 scheme name, not a credential.
    token_type: str = "Bearer"  # noqa: S105


class AuthService:
    """Local authentication and refresh-token lifecycle."""

    def __init__(
        self,
        *,
        session: Session,
        users: UserRepository,
        refresh_tokens: RefreshTokenRepository,
        login_attempts: LoginAttemptRepository,
        issuer: LocalTokenIssuer,
        audit: AuditWriter,
        refresh_ttl_hours: int,
        access_ttl_seconds: int,
        local_login_enabled: bool,
    ) -> None:
        self._session = session
        """Held only so replay containment can commit; see `_handle_unusable_refresh`."""
        self._users = users
        self._refresh_tokens = refresh_tokens
        self._login_attempts = login_attempts
        self._issuer = issuer
        self._audit = audit
        self._refresh_ttl = timedelta(hours=refresh_ttl_hours)
        self._access_ttl_seconds = access_ttl_seconds
        self._local_login_enabled = local_login_enabled

    # ---------------------------------------------------------------- login

    def login(self, *, email: str, password: str, metadata: RequestMetadata) -> TokenPair:
        """Authenticate a local account and issue a token pair."""
        if not self._local_login_enabled:
            raise Unauthenticated("Local password authentication is disabled.")

        normalized = normalize_email(email) or ""
        email_digest = _email_digest(normalized)
        self._enforce_attempt_limit(email_digest)

        user = self._users.get_active_by_email(normalized) if normalized else None
        # Runs even when `user` is None, so the response time does not reveal whether the
        # address exists. `verify_password` does the equivalent work for a missing hash.
        password_ok = verify_password(password, user.password_hash if user is not None else None)
        local_account = user is not None and user.auth_provider == AuthProvider.LOCAL

        if user is None or not local_account or not password_ok:
            self._record_failure(
                email_digest=email_digest,
                user=user,
                metadata=metadata,
                reason=self._failure_reason(user, local_account, password_ok),
            )
            raise Unauthenticated(GENERIC_LOGIN_FAILURE)

        return self._issue_pair(user, metadata=metadata, audit_action=AuditAction.LOGIN_SUCCESS)

    def _failure_reason(self, user: User | None, local_account: bool, password_ok: bool) -> str:
        """A stable code for operational analysis. Never returned to the client."""
        if user is None:
            return "NO_SUCH_USER"
        if not local_account:
            return "NOT_A_LOCAL_ACCOUNT"
        if not password_ok:
            return "BAD_PASSWORD"
        return "UNKNOWN"

    def _enforce_attempt_limit(self, email_digest: str) -> None:
        since = utc_now() - FAILED_ATTEMPT_WINDOW
        failures = self._login_attempts.recent_failure_count(email_hash=email_digest, since=since)
        if failures >= MAX_FAILED_ATTEMPTS_PER_WINDOW:
            raise RateLimited(
                "Too many failed sign-in attempts. Try again later.",
                retry_after_seconds=int(FAILED_ATTEMPT_WINDOW.total_seconds()),
            )

    def _record_failure(
        self,
        *,
        email_digest: str,
        user: User | None,
        metadata: RequestMetadata,
        reason: str,
    ) -> None:
        self._login_attempts.record(
            email_hash=email_digest,
            succeeded=False,
            user_id=user.id if user is not None else None,
            failure_reason=reason,
            ip_address=metadata.ip_address,
            user_agent=metadata.user_agent,
        )
        if user is not None:
            # §20.2 LOGIN_FAILURE needs an actor, which only exists when the user is known.
            # Attempts against unknown addresses live in `login_attempts` alone.
            self._audit.record(
                AuditEvent(
                    action=AuditAction.LOGIN_FAILURE,
                    entity_type="user",
                    entity_id=user.id,
                    actor_user_id=user.id,
                    actor_role_codes="",
                    request_id=metadata.request_id,
                    ip_address=metadata.ip_address,
                    user_agent=metadata.user_agent,
                )
            )

    # ---------------------------------------------------------------- refresh

    def refresh(self, *, refresh_token: str, metadata: RequestMetadata) -> TokenPair:
        """Exchange a refresh token for a new pair, rotating the old one.

        A token that exists but is already revoked means one of two things: a benign retry, or
        a stolen token being replayed. The two are indistinguishable, so the safe response is
        to revoke the user's entire token family and force re-authentication.
        """
        usable = self._refresh_tokens.find_usable(refresh_token)
        if usable is None:
            self._handle_unusable_refresh(refresh_token, metadata=metadata)
            raise Unauthenticated("The refresh token is invalid or has expired.")

        user = self._users.get(usable.user_id)
        if user is None or not user.is_active:
            # §25.5: a deactivated user's tokens are revoked, but a race is still possible.
            self._refresh_tokens.revoke(usable, reason="USER_INACTIVE")
            raise Unauthenticated("The refresh token is invalid or has expired.")

        replacement_token = generate_opaque_token()
        self._refresh_tokens.rotate(
            usable,
            token=replacement_token,
            ttl=self._refresh_ttl,
            user_agent=metadata.user_agent,
            ip_address=metadata.ip_address,
        )
        access_token, _ = self._issuer.issue_access_token(user.id)
        return TokenPair(
            access_token=access_token,
            refresh_token=replacement_token,
            expires_in_seconds=self._access_ttl_seconds,
        )

    def _handle_unusable_refresh(self, refresh_token: str, *, metadata: RequestMetadata) -> None:
        """Revoke the whole token family when a superseded token is replayed.

        **This commits**, which is a deliberate exception to the rule that services leave
        transaction control to :func:`app.db.session.unit_of_work`. The caller raises
        ``401 UNAUTHENTICATED`` immediately after this returns, and that exception rolls the
        transaction back — discarding the very containment this method exists to perform. A
        replay means a refresh token has probably been captured, so cutting the sessions has to
        outlive the failed request.
        """
        existing = self._refresh_tokens.find_any(refresh_token)
        if existing is None or existing.revoked_reason != "ROTATED":
            return
        revoked = self._refresh_tokens.revoke_all_for_user(
            existing.user_id, reason="REPLAY_DETECTED"
        )
        if not revoked:
            return
        self._audit.record(
            AuditEvent(
                action=AuditAction.LOGIN_FAILURE,
                entity_type="user",
                entity_id=existing.user_id,
                actor_user_id=existing.user_id,
                actor_role_codes="",
                reason="Rotated refresh token replayed; all sessions revoked",
                request_id=metadata.request_id,
                ip_address=metadata.ip_address,
                user_agent=metadata.user_agent,
            )
        )
        self._session.commit()

    # ---------------------------------------------------------------- logout

    def logout(self, *, refresh_token: str) -> bool:
        """Revoke one refresh token. Idempotent: an already-revoked token is a no-op.

        Returns whether a live token was revoked. Logout never reports failure to the client —
        a client that has discarded its tokens is logged out regardless.
        """
        existing = self._refresh_tokens.find_usable(refresh_token)
        if existing is None:
            return False
        self._refresh_tokens.revoke(existing, reason="LOGOUT")
        return True

    # ---------------------------------------------------------------- helpers

    def _issue_pair(
        self, user: User, *, metadata: RequestMetadata, audit_action: AuditAction
    ) -> TokenPair:
        refresh_token = generate_opaque_token()
        self._refresh_tokens.issue(
            user_id=user.id,
            token=refresh_token,
            ttl=self._refresh_ttl,
            user_agent=metadata.user_agent,
            ip_address=metadata.ip_address,
        )
        access_token, _ = self._issuer.issue_access_token(user.id)
        self._users.record_login(user)
        self._login_attempts.record(
            email_hash=_email_digest(user.email),
            succeeded=True,
            user_id=user.id,
            ip_address=metadata.ip_address,
            user_agent=metadata.user_agent,
        )
        self._audit.record(
            AuditEvent(
                action=audit_action,
                entity_type="user",
                entity_id=user.id,
                actor_user_id=user.id,
                actor_role_codes="",
                request_id=metadata.request_id,
                ip_address=metadata.ip_address,
                user_agent=metadata.user_agent,
            )
        )
        return TokenPair(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in_seconds=self._access_ttl_seconds,
        )

    @staticmethod
    def hash_new_password(password: str) -> str:
        """Hash a password for storage. Exposed so user creation does not import bcrypt."""
        return hash_password(password)


def _email_digest(email: str) -> str:
    """SHA-256 of a normalised email, for the ``login_attempts`` key.

    Hashed rather than stored plainly so the attempts table does not become a list of
    addresses that were guessed at, which would be a disclosure in its own right.
    """
    return hashlib.sha256(email.encode("utf-8")).hexdigest()


__all__ = [
    "FAILED_ATTEMPT_WINDOW",
    "GENERIC_LOGIN_FAILURE",
    "MAX_FAILED_ATTEMPTS_PER_WINDOW",
    "AuthService",
    "TokenPair",
]
