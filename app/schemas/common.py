"""Shared API contracts: pagination, sorting, cursors, ETags, acknowledgements.

Cursor pagination (§26.5) rather than offset pagination, which the spec forbids outright.
A cursor is an opaque, **signed** token carrying the last row's sort key and id plus an issue
timestamp. Signing is not decoration: an unsigned cursor is attacker-controlled input that
goes straight into a ``WHERE`` clause, so tampering with it would be a way to page outside
the intended result set. Expiry caps how long a stale cursor stays usable (24 hours).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal, Protocol, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator

from app.core.clock import utc_now
from app.core.errors import ValidationFailed
from app.validation.rules import validate_email

DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 100
CURSOR_VERSION = "1"
_SIGNATURE_BYTES = 16
"""128 bits of HMAC output, truncated: enough to make forgery infeasible while keeping the
cursor short enough for a query string."""


class SortDirection(StrEnum):
    """§26.4 sort direction."""

    ASC = "asc"
    DESC = "desc"


class ApiModel(BaseModel):
    """Base for every request and response model.

    ``extra="forbid"`` is a security control, not strictness for its own sake: §1.4 requires
    a ``422 FIELD_READ_ONLY`` when a client supplies a ``SYS`` field, and silently ignoring
    unknown keys would instead let a client believe a field was accepted when it was dropped.
    """

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
        from_attributes=True,
        populate_by_name=True,
    )


def _validate_email_field(value: str) -> str:
    """Apply the §21.1 V-EMAIL rule and return the normalised address.

    Used instead of Pydantic's ``EmailStr``, which additionally rejects special-use and
    reserved domains. That is stricter than the specification: §21.1 asks for a WHATWG
    valid address of at most 254 characters whose domain contains a dot, and an internal
    HR deployment may legitimately use a domain such as ``hr@corp.local``. Diverging from
    the spec to be stricter is still diverging from the spec.
    """
    outcome = validate_email(value)
    if not outcome.ok or outcome.normalized is None:
        raise ValueError(outcome.message or "Not a valid email address.")
    return outcome.normalized


EmailAddress = Annotated[str, AfterValidator(_validate_email_field)]
"""An email address validated and normalised per §21.1 V-EMAIL (trimmed, lower-cased)."""


class VersionedPayload(Protocol):
    """A response payload that carries its resource's ``row_version`` (§24.1).

    Structural rather than a base class: the response models are plain
    :class:`ApiModel` subclasses, and only the handful of helpers that publish an ETag need
    to know that a payload has a version at all.
    """

    row_version: int


class WarningPayload(ApiModel):
    """One §21.3 warning as returned to a client."""

    code: str
    field: str | None = None
    message: str


class AcknowledgementMixin(ApiModel):
    """Mixed into write requests that can raise warnings (§21.3)."""

    acknowledged_warnings: list[str] = Field(
        default_factory=list,
        max_length=50,
        description=(
            "Warning codes the caller has reviewed and accepts. A write that raises an "
            "unacknowledged warning fails with 422 WARNINGS_REQUIRE_ACKNOWLEDGEMENT."
        ),
    )


class ReasonRequest(ApiModel):
    """A body carrying only a reason, used by deactivate/reactivate/archive actions."""

    reason: str = Field(min_length=10, max_length=500)


class PageParams(ApiModel):
    """§26.5 pagination inputs."""

    limit: Annotated[int, Field(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE
    cursor: str | None = Field(default=None, max_length=512)
    with_total: bool = False


class Page[ItemT](ApiModel):
    """§26.5 pagination envelope.

    ``total_count`` is exact and present only when the caller asked and the filtered set is
    small enough; otherwise ``total_count_estimate`` carries an index-statistics estimate, so
    a large tenant never pays for a full count.
    """

    items: list[ItemT]
    next_cursor: str | None = None
    total_count: int | None = None
    total_count_estimate: int | None = None


@dataclass(frozen=True, slots=True)
class CursorPayload:
    """The decoded contents of a cursor."""

    sort_field: str
    sort_value: str | None
    last_id: uuid.UUID
    direction: SortDirection
    issued_at: datetime


class CursorCodec:
    """Encodes, signs and verifies pagination cursors (§26.5).

    Format: ``base64url(json_payload) + "." + base64url(hmac_sha256(payload)[:16])``. The
    HMAC uses the application secret, so a cursor issued by one deployment is not valid in
    another.
    """

    __slots__ = ("_secret", "_ttl_seconds")

    def __init__(self, *, secret: str, ttl_seconds: int) -> None:
        self._secret = secret.encode("utf-8")
        self._ttl_seconds = ttl_seconds

    def encode(
        self,
        *,
        sort_field: str,
        sort_value: Any,
        last_id: uuid.UUID,
        direction: SortDirection,
    ) -> str:
        body = json.dumps(
            {
                "v": CURSOR_VERSION,
                "f": sort_field,
                "s": _serialise_sort_value(sort_value),
                "i": last_id.hex,
                "d": direction.value,
                "t": int(utc_now().timestamp()),
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return f"{_b64encode(body)}.{_b64encode(self._sign(body))}"

    def decode(self, cursor: str) -> CursorPayload:
        """Verify and decode a cursor.

        Any failure — bad base64, bad signature, wrong version, expired, malformed id —
        raises ``422 VALIDATION_FAILED`` with the same message, so a probe learns nothing
        about which part was wrong.
        """
        try:
            encoded_body, encoded_signature = cursor.split(".", 1)
            body = _b64decode(encoded_body)
            signature = _b64decode(encoded_signature)
        except (ValueError, binascii.Error) as exc:
            raise self._invalid() from exc

        if not hmac.compare_digest(signature, self._sign(body)):
            raise self._invalid()

        try:
            payload = json.loads(body)
            issued_at = datetime.fromtimestamp(int(payload["t"]), tz=utc_now().tzinfo)
            decoded = CursorPayload(
                sort_field=str(payload["f"]),
                sort_value=payload["s"],
                last_id=uuid.UUID(hex=str(payload["i"])),
                direction=SortDirection(payload["d"]),
                issued_at=issued_at,
            )
        except (KeyError, ValueError, TypeError) as exc:
            raise self._invalid() from exc

        if str(payload.get("v")) != CURSOR_VERSION:
            raise self._invalid()
        if (utc_now() - issued_at).total_seconds() > self._ttl_seconds:
            raise self._invalid()
        return decoded

    def _sign(self, body: bytes) -> bytes:
        return hmac.new(self._secret, body, hashlib.sha256).digest()[:_SIGNATURE_BYTES]

    @staticmethod
    def _invalid() -> ValidationFailed:
        return ValidationFailed("The pagination cursor is invalid or has expired.").with_detail(
            field="cursor", code="INVALID_CURSOR", message="Restart pagination without a cursor."
        )


def _serialise_sort_value(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime | date):
        return value.isoformat()
    return str(value)


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(encoded: str) -> bytes:
    padding = "=" * (-len(encoded) % 4)
    return base64.urlsafe_b64decode(encoded + padding)


@dataclass(frozen=True, slots=True)
class SortSpec:
    """A validated sort instruction (§26.4)."""

    field: str
    direction: SortDirection

    @classmethod
    def parse(
        cls,
        sort: str | None,
        *,
        allowed: frozenset[str],
        default_field: str,
        default_direction: SortDirection = SortDirection.ASC,
    ) -> Self:
        """Parse ``field`` or ``field:direction``.

        §26.4: "Unknown sort fields -> 422 VALIDATION_FAILED". The allowlist is what keeps a
        caller from sorting by a column they are not permitted to read, which would leak its
        ordering.
        """
        if not sort:
            return cls(field=default_field, direction=default_direction)

        field_name, _, direction_raw = sort.partition(":")
        field_name = field_name.strip()
        if field_name not in allowed:
            raise ValidationFailed(
                f"Unknown sort field. Allowed values: {', '.join(sorted(allowed))}."
            ).with_detail(field="sort", code="UNKNOWN_SORT_FIELD", message=f"{field_name!r}")

        if not direction_raw:
            return cls(field=field_name, direction=default_direction)
        try:
            direction = SortDirection(direction_raw.strip().lower())
        except ValueError as exc:
            raise ValidationFailed("Sort direction must be 'asc' or 'desc'.").with_detail(
                field="sort", code="UNKNOWN_SORT_DIRECTION", message=direction_raw
            ) from exc
        return cls(field=field_name, direction=direction)


def next_cursor_for[RowT](
    codec: CursorCodec,
    *,
    rows: list[RowT],
    has_more: bool,
    sort_field: str,
    direction: SortDirection,
    value_of: Callable[[RowT], Any],
    id_of: Callable[[RowT], uuid.UUID],
) -> str | None:
    """Issue the cursor for the next page, or ``None`` when this page is the last.

    Shared by every list endpoint so the keyset and the token that encodes it cannot drift
    apart: the cursor always carries the *last row's* sort value and id, which is exactly what
    the repository's keyset predicate compares against.
    """
    if not has_more or not rows:
        return None
    last = rows[-1]
    return codec.encode(
        sort_field=sort_field,
        sort_value=value_of(last),
        last_id=id_of(last),
        direction=direction,
    )


def etag_for(row_version: int) -> str:
    """The strong ETag for a resource version (§24.1: "GET returns ETag (= row_version)")."""
    return f'"{row_version}"'


def parse_if_match(header: str | None) -> int | None:
    """Parse an ``If-Match`` header into a row version.

    Accepts the quoted strong form this API emits, a bare integer, and ``W/`` weak prefixes
    that intermediaries sometimes add. Returns ``None`` for a missing header; the caller
    decides whether ``If-Match`` was required. ``*`` means "any current version", which for
    optimistic locking is equivalent to not supplying one.
    """
    if header is None:
        return None
    candidate = header.strip()
    if candidate == "*":
        return None
    if candidate.startswith("W/"):
        candidate = candidate[2:]
    candidate = candidate.strip('"')
    try:
        return int(candidate)
    except ValueError as exc:
        raise ValidationFailed("The If-Match header is not a valid ETag.").with_detail(
            field="If-Match", code="INVALID_ETAG", message="Expected the ETag from a prior GET."
        ) from exc


class HealthResponse(ApiModel):
    """``/health`` — liveness. Reports nothing about configuration or versions."""

    status: Literal["ok"] = "ok"


class ReadinessResponse(ApiModel):
    """``/ready`` — readiness, including database reachability."""

    status: Literal["ready", "degraded"]
    database: Literal["up", "down"]


class IdempotencyKeyHeader(ApiModel):
    """Validation for the ``Idempotency-Key`` header (§24.1: a UUID)."""

    key: uuid.UUID

    @field_validator("key", mode="before")
    @classmethod
    def coerce(cls, value: Any) -> Any:
        if isinstance(value, str):
            try:
                return uuid.UUID(value)
            except ValueError as exc:
                raise ValueError("Idempotency-Key must be a UUID") from exc
        return value


__all__ = [
    "CURSOR_VERSION",
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "AcknowledgementMixin",
    "ApiModel",
    "CursorCodec",
    "CursorPayload",
    "EmailAddress",
    "HealthResponse",
    "IdempotencyKeyHeader",
    "Page",
    "PageParams",
    "ReadinessResponse",
    "ReasonRequest",
    "SortDirection",
    "SortSpec",
    "VersionedPayload",
    "WarningPayload",
    "etag_for",
    "next_cursor_for",
    "parse_if_match",
]
