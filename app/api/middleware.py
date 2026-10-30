"""HTTP middleware: correlation ids, access logging and security headers."""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from app.core.logging import (
    REQUEST_ID_HEADER,
    bind_request_context,
    clear_request_context,
    generate_request_id,
)

logger = logging.getLogger("app.access")

MAX_CLIENT_REQUEST_ID_LENGTH = 64
"""§20.1 stores ``request_id`` as VARCHAR(64), so a longer client value is replaced."""

SECURITY_HEADERS: dict[str, str] = {
    # NFR-11: TLS 1.2+ with HSTS. TLS itself is terminated at the edge; this header is what
    # the application contributes.
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    # §25.9 asks for a strict CSP in the UI. For a JSON API the safest policy is to forbid
    # everything: a response rendered in a browser can then execute nothing.
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'; base-uri 'none'",
}

DOCS_UI_PATHS = frozenset({"/docs", "/docs/oauth2-redirect", "/redoc"})
"""The three HTML pages FastAPI serves for the interactive OpenAPI UI."""

DOCS_CONTENT_SECURITY_POLICY = (
    "default-src 'none'; "
    # Swagger UI and ReDoc are loaded from jsDelivr and bootstrapped by an inline script,
    # which `default-src 'none'` blocks outright -- the page loads but renders nothing.
    "script-src https://cdn.jsdelivr.net 'unsafe-inline'; "
    "style-src https://cdn.jsdelivr.net https://fonts.googleapis.com 'unsafe-inline'; "
    "font-src https://fonts.gstatic.com data:; "
    "img-src https://fastapi.tiangolo.com data:; "
    # "Try it out" calls this API; ReDoc renders the schema in a blob: worker.
    "connect-src 'self'; "
    "worker-src blob:; "
    "frame-ancestors 'none'; base-uri 'none'"
)
"""The narrowest policy under which the bundled UI actually runs.

It applies to :data:`DOCS_UI_PATHS` only -- three static documentation pages that carry no
tenant data. Every API response keeps the `default-src 'none'` policy of §25.9. Self-hosting
the two bundles would remove the CDN and `unsafe-inline` from this list; see DEPLOYMENT.md.
"""

NO_STORE_CACHE_CONTROL = "no-store, no-cache, must-revalidate, private"
"""Applied to every API response.

§11.6 and §15.6 require ``no-store`` on reveal and download responses specifically. Applying
it to the whole API is strictly safer and costs nothing: every endpoint here returns
tenant-scoped personal data, none of which should sit in a shared cache or a browser's disk
cache.
"""


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assigns a correlation id, logs the request, and clears context afterwards.

    A client-supplied ``X-Request-Id`` is honoured so a caller can trace a request end to
    end, but it is validated first: it is echoed in responses and stored in audit rows, so an
    unbounded or control-character-laden value would be a log-injection vector.
    """

    def __init__(self, app: ASGIApp, *, api_prefix: str) -> None:
        super().__init__(app)
        self._api_prefix = api_prefix

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = _resolve_request_id(request.headers.get(REQUEST_ID_HEADER))
        bind_request_context(request_id=request_id)
        request.state.request_id = request_id
        started = time.perf_counter()

        try:
            response = await call_next(request)
        except Exception:
            # The exception handler produces the response; this only records timing so a
            # failed request still appears in the access log with its duration.
            logger.warning(
                "request failed",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                },
            )
            # Deliberately *not* cleared here. An unhandled exception is handled by
            # Starlette's ServerErrorMiddleware, which sits outside this middleware, so
            # clearing now would strip the correlation id from the 500's own log record --
            # the one record that most needs to be traceable. Context variables are
            # task-local, and the next request on this task rebinds them, so nothing leaks.
            raise

        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        response.headers[REQUEST_ID_HEADER] = request_id
        # Never log the query string: §26.2 allows an identifier value as a search parameter,
        # and the path alone is enough to attribute a request.
        logger.info(
            "request completed",
            extra={
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "duration_ms": duration_ms,
            },
        )
        clear_request_context()
        return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Adds the headers in :data:`SECURITY_HEADERS` and a no-store cache policy."""

    def __init__(self, app: ASGIApp, *, api_prefix: str) -> None:
        super().__init__(app)
        self._api_prefix = api_prefix

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        if request.url.path in DOCS_UI_PATHS:
            # Set first, so the loop below leaves it alone: `setdefault` keeps the first
            # value written, here and for an endpoint that set its own policy.
            response.headers.setdefault("Content-Security-Policy", DOCS_CONTENT_SECURITY_POLICY)
        for header, value in SECURITY_HEADERS.items():
            response.headers.setdefault(header, value)
        if request.url.path.startswith(self._api_prefix):
            # setdefault, so an endpoint that already set its own policy keeps it.
            response.headers.setdefault("Cache-Control", NO_STORE_CACHE_CONTROL)
            response.headers.setdefault("Pragma", "no-cache")
        return response


def _resolve_request_id(supplied: str | None) -> str:
    """Accept a safe client-supplied correlation id, or generate one."""
    if not supplied:
        return generate_request_id()
    candidate = supplied.strip()
    if (
        not candidate
        or len(candidate) > MAX_CLIENT_REQUEST_ID_LENGTH
        or not all(character.isalnum() or character in "-_" for character in candidate)
    ):
        return generate_request_id()
    return candidate


def new_correlation_id() -> str:
    """A correlation id for work started outside a request, such as a scheduled job."""
    return uuid.uuid4().hex


__all__ = [
    "DOCS_CONTENT_SECURITY_POLICY",
    "DOCS_UI_PATHS",
    "MAX_CLIENT_REQUEST_ID_LENGTH",
    "NO_STORE_CACHE_CONTROL",
    "SECURITY_HEADERS",
    "RequestContextMiddleware",
    "SecurityHeadersMiddleware",
    "new_correlation_id",
]
