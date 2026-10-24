"""Cross-cutting middleware."""
from __future__ import annotations

import json
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response


class IdempotencyMiddleware(BaseHTTPMiddleware):
    """Replay the stored response for repeated POSTs carrying an Idempotency-Key.

    In-memory implementation suitable for a single process. Swap ``self._cache``
    for Redis in a multi-instance deployment.
    """

    def __init__(self, app, ttl_seconds: int = 24 * 3600) -> None:
        super().__init__(app)
        self.ttl_seconds = ttl_seconds
        self._cache: dict[str, tuple[float, int, object]] = {}

    def _gc(self, now: float) -> None:
        expired = [key for key, (ts, _, _) in self._cache.items() if now - ts >= self.ttl_seconds]
        for key in expired:
            self._cache.pop(key, None)

    async def dispatch(self, request: Request, call_next):
        key = request.headers.get("idempotency-key")
        if request.method != "POST" or not key:
            return await call_next(request)

        now = time.time()
        self._gc(now)
        cached = self._cache.get(key)
        if cached is not None and now - cached[0] < self.ttl_seconds:
            return JSONResponse(
                status_code=cached[1],
                content=cached[2],
                headers={"Idempotency-Replayed": "true"},
            )

        response = await call_next(request)
        content_type = response.headers.get("content-type", "")
        if content_type.startswith("application/json"):
            body = b""
            async for chunk in response.body_iterator:
                body += chunk
            try:
                payload = json.loads(body)
                self._cache[key] = (now, response.status_code, payload)
            except (ValueError, TypeError):
                pass
            headers = {k: v for k, v in response.headers.items() if k.lower() != "content-length"}
            return Response(
                content=body,
                status_code=response.status_code,
                headers=headers,
                media_type=response.media_type,
            )
        return response
