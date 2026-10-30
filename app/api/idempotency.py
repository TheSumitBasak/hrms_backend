"""``Idempotency-Key`` handling for the create endpoints that require it (§24.1, JA §13.11).

JA §13.11 names four endpoints where a retry must not create a second row: positions,
headcount plans, headcount change requests and reservations. Each of them either consumes a
unique code or authorizes capacity, so a duplicate is not merely untidy.

The guard works in three steps, and the middle one is inside the caller's transaction:

1. :meth:`IdempotencyGuard.replay` looks for a completed row with the same key. Same key and
   same payload returns the stored response verbatim; same key with a *different* payload is
   ``409 IDEMPOTENCY_KEY_REUSED``, because that is a client bug and silently treating it as a
   new request would hide it.
2. :meth:`IdempotencyGuard.claim` inserts the key **before** the work runs, so two concurrent
   duplicates collide on the unique constraint instead of both proceeding.
3. :meth:`IdempotencyGuard.complete` stores the response body.

Claim and complete share the business transaction deliberately: if the create fails and rolls
back, the key disappears with it and the client may retry. A key that outlived a failed
request would permanently lock the client out of a create it never completed.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import timedelta
from typing import Any

from app.core.context import OrgContext
from app.core.errors import IdempotencyKeyReused, ValidationFailed
from app.models.platform import IdempotencyKey
from app.repositories.platform import IdempotencyRepository

IDEMPOTENCY_TTL = timedelta(hours=24)
"""§24.1: a replay within 24 hours returns the original response."""


def request_fingerprint(payload: Any) -> str:
    """A stable SHA-256 over the request body.

    Canonicalised with sorted keys, so re-ordered JSON keys are the *same* request rather
    than a spurious conflict.
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class IdempotencyGuard:
    """One idempotent create. Built per request by :func:`idempotency_guard`."""

    __slots__ = ("_fingerprint", "_key", "_method", "_path", "_repository", "_row", "_user_id")

    def __init__(
        self,
        *,
        repository: IdempotencyRepository,
        user_id: uuid.UUID,
        key: uuid.UUID,
        method: str,
        path: str,
        payload: Any,
    ) -> None:
        self._repository = repository
        self._user_id = user_id
        self._key = key
        self._method = method
        self._path = path
        self._fingerprint = request_fingerprint(payload)
        self._row: IdempotencyKey | None = None

    def replay(self) -> dict[str, Any] | None:
        """The stored response for this key, or ``None`` when it is a fresh request."""
        existing = self._repository.find(user_id=self._user_id, key=self._key)
        if existing is None:
            return None
        if existing.request_fingerprint != self._fingerprint:
            raise IdempotencyKeyReused(
                "That Idempotency-Key was already used with a different request body."
            ).with_detail(
                field="Idempotency-Key",
                code="IDEMPOTENCY_KEY_REUSED",
                message="Use a new key for a different request.",
            )
        if existing.response_body is None:
            # An earlier attempt claimed the key and has not finished. Treating this as a
            # conflict is safer than running the work twice.
            raise IdempotencyKeyReused("A request with that Idempotency-Key is still in progress.")
        decoded: dict[str, Any] = json.loads(existing.response_body)
        return decoded

    def claim(self) -> None:
        """Reserve the key inside the caller's transaction, before the work runs."""
        self._row = self._repository.begin(
            user_id=self._user_id,
            key=self._key,
            method=self._method,
            path=self._path,
            fingerprint=self._fingerprint,
            ttl=IDEMPOTENCY_TTL,
        )

    def complete(self, *, status_code: int, body: Any) -> None:
        """Record the response so a replay can return it unchanged."""
        if self._row is None:  # pragma: no cover - defensive; claim() always precedes this
            raise RuntimeError("complete() called before claim()")
        self._repository.complete(
            self._row,
            status_code=status_code,
            body=json.dumps(body, sort_keys=True, separators=(",", ":"), default=str),
        )


def require_idempotency_key(key: uuid.UUID | None, *, endpoint: str) -> uuid.UUID:
    """Insist on the header where JA §13.11 requires it."""
    if key is None:
        raise ValidationFailed(f"{endpoint} requires an Idempotency-Key header.").with_detail(
            field="Idempotency-Key",
            code="REQUIRED",
            message="Supply a UUID so the create is safely retryable.",
        )
    return key


def idempotency_guard(
    *,
    repository: IdempotencyRepository,
    context: OrgContext,
    key: uuid.UUID,
    method: str,
    path: str,
    payload: Any,
) -> IdempotencyGuard:
    return IdempotencyGuard(
        repository=repository,
        user_id=context.user_id,
        key=key,
        method=method,
        path=path,
        payload=payload,
    )


__all__ = [
    "IDEMPOTENCY_TTL",
    "IdempotencyGuard",
    "idempotency_guard",
    "request_fingerprint",
    "require_idempotency_key",
]
