"""Custom SQLAlchemy column types for the §1.4 logical type table.

| Logical type | MySQL         | Implemented by            |
|--------------|---------------|---------------------------|
| UUID         | `BINARY(16)`  | :class:`Uuid7`            |
| TIMESTAMP    | `DATETIME(6)` | :class:`UtcDateTime`      |
| BOOLEAN      | `TINYINT(1)`  | SQLAlchemy ``Boolean``    |
| DECIMAL(p,s) | `DECIMAL(p,s)`| :func:`money`, :func:`percentage` |
| BYTES        | `VARBINARY(n)`| SQLAlchemy ``LargeBinary``|

UUID keys are **UUIDv7** (ADR-004): the leading 48 bits are a millisecond timestamp, so keys
generated over time are monotonically increasing. That matters on MySQL specifically, because
InnoDB clusters rows on the primary key — random UUIDv4 keys would scatter inserts across the
B-tree and fragment it badly at the one-million-row scale of NFR-17.
"""

from __future__ import annotations

import os
import time
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import BINARY, VARBINARY, DateTime, Dialect, Numeric
from sqlalchemy.dialects import mysql
from sqlalchemy.types import TypeDecorator

# ---------------------------------------------------------------------------- UUIDv7

_UUID7_VERSION = 0x7000
_VARIANT_RFC4122 = 0x8000


def uuid7() -> uuid.UUID:
    """Generate a UUIDv7 (RFC 9562): 48-bit millisecond timestamp plus 74 random bits.

    Time-ordered, so it is index-friendly, while still carrying enough randomness that an id
    cannot be guessed from a neighbouring one. The timestamp is a creation hint only; it is
    never read back as business data.
    """
    timestamp_ms = time.time_ns() // 1_000_000
    random_bytes = os.urandom(10)

    # rand_a: 12 bits, sharing its 16-bit field with the 4-bit version.
    rand_a = int.from_bytes(random_bytes[:2], "big") & 0x0FFF
    # rand_b: 62 bits, sharing its first 16-bit field with the 2-bit variant.
    rand_b_high = int.from_bytes(random_bytes[2:4], "big") & 0x3FFF
    rand_b_low = int.from_bytes(random_bytes[4:10], "big")

    value = (
        (timestamp_ms & 0xFFFF_FFFF_FFFF) << 80
        | (_UUID7_VERSION | rand_a) << 64
        | (_VARIANT_RFC4122 | rand_b_high) << 48
        | rand_b_low
    )
    return uuid.UUID(int=value)


class Uuid7(TypeDecorator[uuid.UUID]):
    """A UUID stored as ``BINARY(16)`` (§1.4).

    Accepts a :class:`uuid.UUID` or its string form on the way in and always returns a
    :class:`uuid.UUID`, so service code never deals with raw bytes or with the
    string/UUID ambiguity that leads to comparisons silently failing.
    """

    impl = BINARY(16)
    cache_ok = True
    python_type = uuid.UUID

    def process_bind_param(self, value: Any, dialect: Dialect) -> bytes | None:
        if value is None:
            return None
        if isinstance(value, uuid.UUID):
            return value.bytes
        if isinstance(value, bytes):
            if len(value) != 16:
                raise ValueError("a UUID column requires exactly 16 bytes")
            return value
        if isinstance(value, str):
            return uuid.UUID(value).bytes
        raise TypeError(f"cannot bind {type(value).__name__} to a UUID column")

    def process_result_value(self, value: Any, dialect: Dialect) -> uuid.UUID | None:
        if value is None:
            return None
        return value if isinstance(value, uuid.UUID) else uuid.UUID(bytes=bytes(value))


class UtcDateTime(TypeDecorator[datetime]):
    """A timestamp stored in UTC as ``DATETIME(6)`` (§1.4).

    MySQL ``DATETIME`` carries no timezone, so this type enforces the convention at the
    boundary: naive values are rejected on write rather than silently stored in whatever the
    caller meant, and values read back are always tagged UTC. That makes "all event
    timestamps in UTC" a property of the schema instead of a habit.
    """

    impl = DateTime(timezone=False)
    cache_ok = True
    python_type = datetime

    def load_dialect_impl(self, dialect: Dialect) -> Any:
        if dialect.name == "mysql":
            # fsp=6 keeps microsecond precision; MySQL truncates to seconds otherwise.
            return dialect.type_descriptor(mysql.DATETIME(fsp=6))
        return dialect.type_descriptor(DateTime(timezone=True))

    def process_bind_param(self, value: Any, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if not isinstance(value, datetime):
            raise TypeError("expected a datetime")
        if value.tzinfo is None:
            raise ValueError(
                "naive datetime rejected: timestamps must be timezone-aware and are stored "
                "in UTC (§1.4)"
            )
        as_utc = value.astimezone(UTC)
        return as_utc if dialect.name != "mysql" else as_utc.replace(tzinfo=None)

    def process_result_value(self, value: Any, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if not isinstance(value, datetime):
            raise TypeError("expected a datetime from the database")
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


# ---------------------------------------------------------------------------- numerics


def money() -> Numeric[Decimal]:
    """``DECIMAL(15,2)`` for monetary reference values (§1.4, §23.1).

    Returns :class:`~decimal.Decimal`, never float: §4.3 forbids computation over these
    values, and binary floating point would additionally corrupt the stored figure.
    """
    return Numeric(precision=15, scale=2, asdecimal=True)


def percentage() -> Numeric[Decimal]:
    """``DECIMAL(7,4)`` for salary component percentages (§1.4, §16.2).

    A stored label only. §4.3: a percentage is never applied to a base amount.
    """
    return Numeric(precision=7, scale=4, asdecimal=True)


def share_percentage() -> Numeric[Decimal]:
    """``DECIMAL(5,2)`` for nominee shares and disability percentages (§13.2, §10.4)."""
    return Numeric(precision=5, scale=2, asdecimal=True)


def ciphertext(max_bytes: int = 512) -> VARBINARY:
    """``VARBINARY(n)`` for an AES-256-GCM sealed field value (§1.4 ``BYTES``).

    **Not** ``LargeBinary``: that maps to ``BLOB`` on MySQL, which cannot participate in an
    index without a prefix length and is stored off-page. Sealed values are short — a 12-byte
    nonce, the plaintext, and a 16-byte tag — so ``VARBINARY`` keeps them in-row.
    """
    return VARBINARY(max_bytes)


def blind_index() -> VARBINARY:
    """``VARBINARY(32)`` for an HMAC-SHA256 blind index (§11.1 ``BYTES(32)``).

    Must be ``VARBINARY`` rather than ``LargeBinary``: every duplicate check and identifier
    search is an indexed equality lookup on this column (§11.5, §26.2), and MySQL refuses to
    index a ``BLOB`` without a key length.
    """
    return VARBINARY(32)


MONEY_SCALE = Decimal("0.01")
SHARE_SCALE = Decimal("0.01")
FULL_ALLOCATION = Decimal("100.00")
"""§13.3: active nominee shares must total exactly this when full allocation is required."""


__all__ = [
    "FULL_ALLOCATION",
    "MONEY_SCALE",
    "SHARE_SCALE",
    "UtcDateTime",
    "Uuid7",
    "blind_index",
    "ciphertext",
    "money",
    "percentage",
    "share_percentage",
    "uuid7",
]
