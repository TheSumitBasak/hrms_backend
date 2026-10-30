"""Structured log rendering and the redaction rules that keep personal data out (§25.9)."""

from __future__ import annotations

import json
import logging
import re

import pytest

from app.core.logging import (
    REDACTED,
    JsonFormatter,
    RedactingFilter,
    TextFormatter,
    bind_request_context,
    clear_request_context,
)

pytestmark = pytest.mark.unit

_ISO_MILLIS_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


def _record(message: str = "hello", **extra: object) -> logging.LogRecord:
    record = logging.LogRecord(
        name="app.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=None,
        exc_info=None,
    )
    record.__dict__.update(extra)
    return record


@pytest.fixture(autouse=True)
def _clean_context() -> None:
    clear_request_context()


class TestJsonFormatter:
    def test_record_renders_as_single_line_json(self) -> None:
        rendered = JsonFormatter().format(_record())
        assert "\n" not in rendered
        payload = json.loads(rendered)
        assert payload["level"] == "INFO"
        assert payload["logger"] == "app.test"
        assert payload["message"] == "hello"

    def test_timestamp_is_utc_iso8601_with_milliseconds(self) -> None:
        """Regression: the timestamp was built with a `%03d` in a `strftime` format string,
        which `logging.Formatter.formatTime` rejects — every record failed to emit."""
        payload = json.loads(JsonFormatter().format(_record()))
        assert _ISO_MILLIS_UTC.match(payload["timestamp"]), payload["timestamp"]

    def test_emitting_a_record_does_not_raise(self, capsys: pytest.CaptureFixture[str]) -> None:
        """`logging` swallows formatter errors onto stderr, so assert on the stream itself."""
        handler = logging.StreamHandler()
        handler.setFormatter(JsonFormatter())
        logger = logging.getLogger("app.test.emit")
        logger.propagate = False
        logger.addHandler(handler)
        try:
            logger.error("boom")
        finally:
            logger.removeHandler(handler)
        captured = capsys.readouterr()
        assert "Logging error" not in captured.err
        assert json.loads(captured.err.strip())["message"] == "boom"

    def test_correlation_context_is_attached(self) -> None:
        bind_request_context(request_id="r-1", actor_id="a-1", organization_id="o-1")
        payload = json.loads(JsonFormatter().format(_record()))
        assert payload["request_id"] == "r-1"
        assert payload["actor_id"] == "a-1"
        assert payload["organization_id"] == "o-1"

    def test_extra_fields_land_under_context(self) -> None:
        payload = json.loads(JsonFormatter().format(_record(route="/api/v1/users")))
        assert payload["context"]["route"] == "/api/v1/users"

    def test_exception_text_goes_to_the_log(self) -> None:
        try:
            raise RuntimeError("inner")
        except RuntimeError:
            record = _record()
            import sys

            record.exc_info = sys.exc_info()
        payload = json.loads(JsonFormatter().format(record))
        assert "RuntimeError: inner" in payload["exception"]


class TestRedactingFilter:
    def test_bearer_token_is_scrubbed(self) -> None:
        record = _record("auth header was Bearer abc.def.ghi")
        assert RedactingFilter().filter(record) is True
        assert "abc.def.ghi" not in str(record.msg)
        assert REDACTED in str(record.msg)

    def test_sensitive_extra_key_is_scrubbed(self) -> None:
        record = _record("ok", password="hunter2")
        RedactingFilter().filter(record)
        assert record.__dict__["password"] == REDACTED


class TestTextFormatter:
    def test_request_id_is_appended_when_bound(self) -> None:
        bind_request_context(request_id="r-9")
        assert "[request_id=r-9]" in TextFormatter().format(_record())

    def test_no_request_id_suffix_without_context(self) -> None:
        assert "request_id" not in TextFormatter().format(_record())
