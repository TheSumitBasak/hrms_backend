"""FastAPI exception handlers rendering the §24.2 error envelope.

Every error leaves the application in the same shape:

```json
{"error": {"code": "...", "message": "...", "details": [...],
           "warnings": [...], "correlation_id": "..."}}
```

Four handlers cover everything:

* :func:`ems_error_handler` — our own domain errors, which already carry a code and status.
* :func:`validation_error_handler` — FastAPI/Pydantic request validation, mapped to
  ``422 VALIDATION_FAILED`` with one ``details`` entry per field.
* :func:`http_exception_handler` — Starlette's own 404s and 405s, so a wrong path still
  returns the envelope rather than FastAPI's ``{"detail": ...}``.
* :func:`unhandled_exception_handler` — anything else, logged with a traceback and returned
  as an opaque ``500 INTERNAL_ERROR``.

The last one is the security-relevant case: §8 of the brief and OWASP A10 require that no
stack trace, token, key, raw identifier or database error reaches a client. The client gets a
correlation id; the diagnosis lives in the log.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.errors import ERROR_HTTP_STATUS, EmsError, ErrorCode
from app.core.logging import current_request_id

logger = logging.getLogger(__name__)

# Starlette status codes that already correspond to a spec error code.
_HTTP_STATUS_TO_CODE: dict[int, ErrorCode] = {
    400: ErrorCode.MALFORMED_REQUEST,
    401: ErrorCode.UNAUTHENTICATED,
    403: ErrorCode.PERMISSION_DENIED,
    404: ErrorCode.RESOURCE_NOT_FOUND,
    405: ErrorCode.MALFORMED_REQUEST,
    412: ErrorCode.PRECONDITION_FAILED,
    413: ErrorCode.FILE_TOO_LARGE,
    415: ErrorCode.FILE_TYPE_NOT_ALLOWED,
    422: ErrorCode.VALIDATION_FAILED,
    429: ErrorCode.RATE_LIMITED,
}

_GENERIC_HTTP_MESSAGES: dict[int, str] = {
    404: "The requested resource was not found.",
    405: "That method is not allowed for this path.",
}


def build_envelope(
    *,
    code: ErrorCode | str,
    message: str,
    details: list[dict[str, Any]] | None = None,
    warnings: list[dict[str, Any]] | None = None,
    correlation_id: str | None = None,
) -> dict[str, Any]:
    """Construct the §24.2 envelope body."""
    return {
        "error": {
            "code": str(code),
            "message": message,
            "details": details or [],
            "warnings": warnings or [],
            "correlation_id": correlation_id or current_request_id(),
        }
    }


def _correlation_id(request: Request) -> str | None:
    """The correlation id for this request.

    Reads ``request.state`` first and the context variable second. The state carries it even
    for a 500: Starlette handles an unhandled exception in ServerErrorMiddleware, which sits
    *outside* our middleware stack, so the context variable has already been unset by then.
    Without this fallback the one response that most needs a traceable id would not have one.
    """
    return getattr(request.state, "request_id", None) or current_request_id()


def ems_error_handler(request: Request, exc: EmsError) -> JSONResponse:
    """Render a domain error.

    Logged at WARNING for client-correctable failures and at INFO for conflicts that are an
    expected part of optimistic concurrency, so a retry storm does not look like an outage.
    """
    level = logging.INFO if exc.is_retryable else logging.WARNING
    logger.log(
        level,
        "request rejected",
        extra={
            "error_code": exc.code.value,
            "http_status": exc.http_status,
            "path": request.url.path,
            "method": request.method,
        },
    )
    return JSONResponse(
        status_code=exc.http_status,
        content=build_envelope(
            code=exc.code,
            message=exc.message,
            details=[detail.as_dict() for detail in exc.details],
            warnings=[warning.as_dict() for warning in exc.warnings],
            correlation_id=_correlation_id(request),
        ),
        headers=exc.headers or None,
    )


def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Map **request** validation failures to ``422 VALIDATION_FAILED`` with field details.

    Deliberately narrowed to :class:`RequestValidationError`. A bare
    ``pydantic.ValidationError`` raised inside the application — settings that fail to
    load, a response model that does not match — is an internal fault, not something the
    client got wrong. Handling it here returned ``422`` and leaked the offending field
    name and message; those now fall through to the 500 handler, which logs the detail and
    returns nothing but a correlation id (§24.2, §8 of the brief).
    """
    details: list[dict[str, Any]] = []
    for error in exc.errors():
        location = [str(part) for part in error.get("loc", ()) if part not in ("body", "query")]
        details.append(
            {
                "field": ".".join(location) or None,
                "code": str(error.get("type", "invalid")).upper(),
                # `msg` is Pydantic's own text and never echoes the submitted value, so it is
                # safe to return; `input` deliberately is not included (§25.9).
                "message": str(error.get("msg", "Invalid value.")),
            }
        )
    logger.warning(
        "request validation failed",
        extra={"path": request.url.path, "method": request.method, "field_count": len(details)},
    )
    return JSONResponse(
        status_code=ERROR_HTTP_STATUS[ErrorCode.VALIDATION_FAILED],
        content=build_envelope(
            code=ErrorCode.VALIDATION_FAILED,
            message="The request contains invalid field values.",
            details=details,
            correlation_id=_correlation_id(request),
        ),
    )


def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    """Render framework-level HTTP errors in the §24.2 envelope."""
    code = _HTTP_STATUS_TO_CODE.get(exc.status_code, ErrorCode.INTERNAL_ERROR)
    # Starlette's default detail is a short, generic phrase, but a route could pass something
    # more specific; prefer our own generic text for the statuses that reveal routing shape.
    message = _GENERIC_HTTP_MESSAGES.get(exc.status_code) or str(exc.detail)
    return JSONResponse(
        status_code=exc.status_code,
        content=build_envelope(code=code, message=message, correlation_id=_correlation_id(request)),
        headers=getattr(exc, "headers", None),
    )


def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Last resort: log with a traceback, return an opaque 500.

    The response carries no exception type, no message and no traceback — only the
    correlation id, which is enough to find the logged detail.
    """
    logger.exception(
        "unhandled exception",
        extra={"path": request.url.path, "method": request.method},
    )
    return JSONResponse(
        status_code=ERROR_HTTP_STATUS[ErrorCode.INTERNAL_ERROR],
        content=build_envelope(
            code=ErrorCode.INTERNAL_ERROR,
            message="An internal error occurred. Quote the correlation id when reporting it.",
            correlation_id=_correlation_id(request),
        ),
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Install all handlers on ``app``."""
    app.add_exception_handler(EmsError, ems_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, validation_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, unhandled_exception_handler)


__all__ = [
    "build_envelope",
    "ems_error_handler",
    "http_exception_handler",
    "register_exception_handlers",
    "unhandled_exception_handler",
    "validation_error_handler",
]
