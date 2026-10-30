"""Structured logging with correlation ids, and the rules that keep personal data out.

§25.9 is explicit: "Application logs MUST NOT contain personal data, identifiers, salary
values, tokens or document contents; request bodies are not logged." That is enforced here
in three ways rather than left to discipline:

1. The JSON formatter emits a fixed set of keys plus whatever a call site puts in ``extra``.
   There is no "log the whole object" path.
2. :class:`RedactingFilter` scrubs values that look like bearer tokens, long digit runs or
   known sensitive key names from the rendered message and from ``extra`` fields, so an
   accidental interpolation does not become a disclosure.
3. :func:`configure_logging` silences SQLAlchemy's statement logger unless explicitly
   enabled, because bound parameters contain personal data.

The correlation id comes from ``X-Request-Id`` when a client supplies one and is otherwise
generated; it is attached to every record for the life of the request and returned to the
client, so a support question can be traced without quoting any data (§20.1 ``request_id``).
"""

from __future__ import annotations

import json
import logging
import re
import sys
import uuid
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any, Literal

REQUEST_ID_HEADER = "X-Request-Id"

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
actor_id_var: ContextVar[str | None] = ContextVar("actor_id", default=None)
organization_id_var: ContextVar[str | None] = ContextVar("organization_id", default=None)

# Attributes the stdlib puts on every record; everything else in __dict__ is caller `extra`.
_STANDARD_RECORD_KEYS = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)

SENSITIVE_KEY_FRAGMENTS = (
    "password",
    "secret",
    "token",
    "authorization",
    "cookie",
    "key",
    "aadhaar",
    "pan",
    "uan",
    "esic",
    "passport",
    "account_number",
    "ifsc",
    "salary",
    "plaintext",
    "ciphertext",
)

REDACTED = "[redacted]"

# Bearer tokens, and any run of 8+ digits (Aadhaar, UAN, ESIC, bank accounts, phone numbers).
_BEARER_PATTERN = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]+")
_LONG_DIGIT_RUN = re.compile(r"\b\d{8,}\b")


def generate_request_id() -> str:
    return uuid.uuid4().hex


class RedactingFilter(logging.Filter):
    """Scrubs token-like and identifier-like content from records.

    A safety net, not a licence: call sites are still expected to log identifiers and
    counts rather than values. This exists because one careless f-string in one handler
    would otherwise write personal data to disk permanently.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = _redact_text(str(record.msg))
        if record.args:
            record.args = ()
        for key, value in list(record.__dict__.items()):
            if key in _STANDARD_RECORD_KEYS:
                continue
            record.__dict__[key] = _redact_value(key, value)
        return True


def _redact_text(text: str) -> str:
    text = _BEARER_PATTERN.sub(f"Bearer {REDACTED}", text)
    return _LONG_DIGIT_RUN.sub(REDACTED, text)


def _redact_value(key: str, value: Any) -> Any:
    lowered = key.lower()
    if any(fragment in lowered for fragment in SENSITIVE_KEY_FRAGMENTS):
        # `mysql_error` and similar diagnostic ints are safe and useful; only strings and
        # bytes can carry a secret.
        return REDACTED if isinstance(value, str | bytes) else value
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, bytes):
        return REDACTED
    return value


class JsonFormatter(logging.Formatter):
    """Renders a record as a single-line JSON object with a fixed key set."""

    def formatTime(  # noqa: N802  (stdlib spelling)
        self, record: logging.LogRecord, datefmt: str | None = None
    ) -> str:
        """UTC ISO-8601 with milliseconds. `strftime` has no sub-second directive, so the
        base implementation cannot produce this and raises on a `%03d` in `datefmt`."""
        stamp = datetime.fromtimestamp(record.created, UTC)
        return stamp.isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if (request_id := request_id_var.get()) is not None:
            payload["request_id"] = request_id
        if (actor_id := actor_id_var.get()) is not None:
            payload["actor_id"] = actor_id
        if (organization_id := organization_id_var.get()) is not None:
            payload["organization_id"] = organization_id
        if record.exc_info:
            # The traceback goes to the log, never to the client (§8 of the brief).
            payload["exception"] = self.formatException(record.exc_info)

        extras = {
            key: value
            for key, value in record.__dict__.items()
            if key not in _STANDARD_RECORD_KEYS and not key.startswith("_")
        }
        if extras:
            payload["context"] = extras
        return json.dumps(payload, default=str, ensure_ascii=False)


class TextFormatter(logging.Formatter):
    """Human-readable format for local development."""

    def __init__(self) -> None:
        super().__init__(fmt="%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        request_id = request_id_var.get()
        return f"{base} [request_id={request_id}]" if request_id else base


def configure_logging(
    *,
    level: str = "INFO",
    log_format: Literal["json", "text"] = "json",
    sql_echo: bool = False,
) -> None:
    """Install the root handler. Idempotent, so repeated calls do not duplicate output."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if log_format == "json" else TextFormatter())
    handler.addFilter(RedactingFilter())
    root.addHandler(handler)
    root.setLevel(level)

    # SQLAlchemy's engine logger prints bound parameters, which are personal data (§25.9).
    # It stays at WARNING unless the operator explicitly turned SQL echo on in a non-production
    # environment, which `Settings` forbids for staging and production.
    logging.getLogger("sqlalchemy.engine").setLevel("INFO" if sql_echo else "WARNING")
    logging.getLogger("sqlalchemy.pool").setLevel("WARNING")
    # Access logs are emitted by our own middleware with a correlation id instead.
    logging.getLogger("uvicorn.access").setLevel("WARNING")


def bind_request_context(
    *,
    request_id: str,
    actor_id: str | None = None,
    organization_id: str | None = None,
) -> None:
    """Attach correlation context for the current request."""
    request_id_var.set(request_id)
    actor_id_var.set(actor_id)
    organization_id_var.set(organization_id)


def clear_request_context() -> None:
    """Detach correlation context. Called when a request completes."""
    request_id_var.set(None)
    actor_id_var.set(None)
    organization_id_var.set(None)


def current_request_id() -> str | None:
    return request_id_var.get()


__all__ = [
    "REDACTED",
    "REQUEST_ID_HEADER",
    "SENSITIVE_KEY_FRAGMENTS",
    "JsonFormatter",
    "RedactingFilter",
    "TextFormatter",
    "bind_request_context",
    "clear_request_context",
    "configure_logging",
    "current_request_id",
    "generate_request_id",
]
