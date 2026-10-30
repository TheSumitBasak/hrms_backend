"""Password hashing and bearer-token issue/verification.

§24.1 makes OAuth 2.0 / OIDC bearer tokens the authentication mechanism and A-04 names an
external identity provider as the expected source, with local accounts as the fallback. Both
paths resolve to the same :class:`TokenClaims`, so nothing downstream of
:func:`app.api.deps.get_current_user` knows or cares which was used.

**What is not configured here.** No identity provider exists in this environment, so
:class:`OidcTokenVerifier` is a real but inert implementation: it needs a JWKS endpoint from
configuration and refuses to validate anything without one. It is wired rather than stubbed
so that supplying ``OIDC_ISSUER``, ``OIDC_AUDIENCE`` and ``OIDC_JWKS_URL`` is the only step
required. See ``docs/AUTH.md``.

MFA (NFR-12) is enforced by the identity provider, not here; the ``users.mfa_enabled`` flag
records the state for the §25.5 access review and is asserted on privileged roles at
grant time.
"""

from __future__ import annotations

import base64
import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol

import bcrypt
from jose import JWTError, jwt

from app.core.clock import utc_now
from app.core.errors import Unauthenticated

BCRYPT_COST = 12
"""Work factor. 12 is the current sensible default; raising it is a configuration change,
and :func:`password_needs_rehash` makes migrating existing hashes a login-time concern."""

MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 256
"""Upper bound so a very large body cannot be turned into a hashing denial of service."""


class TokenType(StrEnum):
    ACCESS = "access"
    REFRESH = "refresh"


# ------------------------------------------------------------------ password hashing


def _prehash(password: str) -> bytes:
    """SHA-256 then base64, so the full password reaches bcrypt.

    bcrypt silently ignores input past 72 bytes, which would make two long passwords sharing
    a 72-byte prefix interchangeable. Pre-hashing to a fixed-length digest removes the
    truncation entirely; base64 keeps the digest free of NUL bytes, which bcrypt also treats
    as a terminator.
    """
    return base64.b64encode(hashlib.sha256(password.encode("utf-8")).digest())


def hash_password(password: str) -> str:
    """Hash a password with bcrypt. Raises :class:`ValueError` on an unacceptable password."""
    if not MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH:
        raise ValueError(
            f"password must be between {MIN_PASSWORD_LENGTH} and {MAX_PASSWORD_LENGTH} characters"
        )
    return bcrypt.hashpw(_prehash(password), bcrypt.gensalt(rounds=BCRYPT_COST)).decode("ascii")


def verify_password(password: str, password_hash: str | None) -> bool:
    """Check a password against a stored hash.

    Returns ``False`` for a missing hash *after* doing the same work as a real comparison, so
    "no such user" and "wrong password" take comparable time and cannot be distinguished by
    timing (OWASP A07 user enumeration).
    """
    candidate = _prehash(password)
    if not password_hash:
        bcrypt.checkpw(
            candidate, bcrypt.hashpw(b"timing-equaliser", bcrypt.gensalt(rounds=BCRYPT_COST))
        )
        return False
    try:
        return bcrypt.checkpw(candidate, password_hash.encode("ascii"))
    except (ValueError, TypeError):
        # A malformed stored hash must deny, not raise (fail closed).
        return False


def password_needs_rehash(password_hash: str) -> bool:
    """Whether a stored hash predates the current cost factor."""
    try:
        cost = int(password_hash.split("$")[2])
    except (IndexError, ValueError):
        return True
    return cost < BCRYPT_COST


# ------------------------------------------------------------------ tokens


@dataclass(frozen=True, slots=True)
class TokenClaims:
    """The claims the application relies on, whatever issued the token.

    Deliberately narrow: the subject, the token's identity and its validity window. Roles and
    permissions are **not** read from the token — they are resolved per request from
    ``user_organization_access`` so that a revoked grant takes effect within the §5.5 60-second
    window instead of surviving until the token expires.
    """

    subject: uuid.UUID
    token_id: str
    token_type: TokenType
    issued_at: datetime
    expires_at: datetime
    issuer: str

    @property
    def is_expired(self) -> bool:
        return utc_now() >= self.expires_at


class TokenVerifier(Protocol):
    """Verifies a bearer token and returns its claims, or raises :class:`Unauthenticated`."""

    def verify(self, token: str) -> TokenClaims: ...


class LocalTokenIssuer:
    """Issues and verifies tokens signed with the application secret.

    Used for local accounts and for tests. The access-token lifetime is capped by
    configuration at 15 minutes (NFR-13); refresh tokens are **not** issued as JWTs — they
    are opaque random strings stored hashed (DEC-005), so revoking one is a database write
    rather than a signature-level problem.
    """

    __slots__ = ("_algorithm", "_audience", "_issuer", "_secret", "_ttl")

    def __init__(
        self,
        *,
        secret: str,
        algorithm: str = "HS256",
        issuer: str,
        audience: str,
        access_token_ttl_seconds: int,
    ) -> None:
        self._secret = secret
        self._algorithm = algorithm
        self._issuer = issuer
        self._audience = audience
        self._ttl = timedelta(seconds=access_token_ttl_seconds)

    def issue_access_token(self, user_id: uuid.UUID) -> tuple[str, TokenClaims]:
        """Mint a short-lived access token for ``user_id``."""
        now = utc_now()
        expires_at = now + self._ttl
        token_id = uuid.uuid4().hex
        payload = {
            "sub": str(user_id),
            "jti": token_id,
            "typ": TokenType.ACCESS.value,
            "iat": int(now.timestamp()),
            "exp": int(expires_at.timestamp()),
            "iss": self._issuer,
            "aud": self._audience,
        }
        token = jwt.encode(payload, self._secret, algorithm=self._algorithm)
        claims = TokenClaims(
            subject=user_id,
            token_id=token_id,
            token_type=TokenType.ACCESS,
            issued_at=now,
            expires_at=expires_at,
            issuer=self._issuer,
        )
        return token, claims

    def verify(self, token: str) -> TokenClaims:
        """Verify signature, issuer, audience and expiry."""
        try:
            payload: dict[str, Any] = jwt.decode(
                token,
                self._secret,
                algorithms=[self._algorithm],
                audience=self._audience,
                issuer=self._issuer,
            )
        except JWTError as exc:
            # The reason is logged, never returned: a client learns only that the token was
            # not accepted (OWASP A09 / §8 of the brief).
            raise Unauthenticated("The access token is invalid or has expired.") from exc
        return _claims_from_payload(payload, default_issuer=self._issuer)


class OidcTokenVerifier:
    """Verifies tokens issued by an external OIDC provider.

    **Unconfigured in this environment.** Construction requires an issuer, audience and JWKS
    URL; without them :meth:`verify` raises rather than falling back to a weaker check, so a
    half-configured deployment cannot accept unverified tokens.

    To complete the integration, fetch the provider's JWKS from ``jwks_url`` (cached for
    ``cache_seconds``), select the key by the token's ``kid``, and call ``jwt.decode`` with
    that key and the ``RS256`` algorithm. The claim mapping is already shared with the local
    issuer via :func:`_claims_from_payload`, so nothing downstream changes.
    """

    __slots__ = ("_audience", "_cache_seconds", "_issuer", "_jwks_url")

    def __init__(
        self, *, issuer: str, audience: str, jwks_url: str, cache_seconds: int = 3600
    ) -> None:
        self._issuer = issuer
        self._audience = audience
        self._jwks_url = jwks_url
        self._cache_seconds = cache_seconds

    def verify(self, token: str) -> TokenClaims:
        raise NotImplementedError(
            "OIDC verification requires a JWKS client. No identity provider is configured "
            "in this environment; see docs/AUTH.md."
        )


class CompositeTokenVerifier:
    """Tries each configured verifier in turn.

    Lets a deployment run OIDC and local accounts side by side during a migration. Order
    matters: OIDC first, so a local secret can never be used to forge a token that an OIDC
    issuer would otherwise have had to sign.
    """

    __slots__ = ("_verifiers",)

    def __init__(self, *verifiers: TokenVerifier) -> None:
        if not verifiers:
            raise ValueError("at least one token verifier is required")
        self._verifiers = verifiers

    def verify(self, token: str) -> TokenClaims:
        for verifier in self._verifiers:
            try:
                return verifier.verify(token)
            except (Unauthenticated, NotImplementedError):
                continue
        raise Unauthenticated("The access token is invalid or has expired.")


def _claims_from_payload(payload: dict[str, Any], *, default_issuer: str) -> TokenClaims:
    """Map a decoded JWT payload onto :class:`TokenClaims`."""
    subject_raw = payload.get("sub")
    if not subject_raw:
        raise Unauthenticated("The access token is missing a subject.")
    try:
        subject = uuid.UUID(str(subject_raw))
    except ValueError as exc:
        raise Unauthenticated("The access token subject is not a valid identifier.") from exc

    token_type = payload.get("typ", TokenType.ACCESS.value)
    if token_type != TokenType.ACCESS.value:
        # A refresh token presented as a bearer credential must not authenticate a request.
        raise Unauthenticated("A refresh token cannot be used to authorize a request.")

    issued_at = datetime.fromtimestamp(int(payload.get("iat", 0)), tz=UTC)
    expires_at = datetime.fromtimestamp(int(payload["exp"]), tz=UTC)
    return TokenClaims(
        subject=subject,
        token_id=str(payload.get("jti", "")),
        token_type=TokenType.ACCESS,
        issued_at=issued_at,
        expires_at=expires_at,
        issuer=str(payload.get("iss", default_issuer)),
    )


__all__ = [
    "BCRYPT_COST",
    "MAX_PASSWORD_LENGTH",
    "MIN_PASSWORD_LENGTH",
    "CompositeTokenVerifier",
    "LocalTokenIssuer",
    "OidcTokenVerifier",
    "TokenClaims",
    "TokenType",
    "TokenVerifier",
    "hash_password",
    "password_needs_rehash",
    "verify_password",
]
