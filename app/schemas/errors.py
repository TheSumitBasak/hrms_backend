"""OpenAPI-only models for the §24.2 error envelope (BE-C01).

The runtime envelope is built by ``app.core.exception_handlers``; these models exist so the
published contract describes exactly that shape instead of FastAPI's default
``HTTPValidationError``.
"""

from __future__ import annotations

from app.schemas.common import ApiModel


class ErrorDetailItem(ApiModel):
    field: str | None = None
    """Dotted path with ``[n]`` indices, e.g. ``components[2].value_amount``."""

    code: str
    message: str


class WarningItem(ApiModel):
    code: str
    field: str | None = None
    message: str


class ErrorBody(ApiModel):
    code: str
    message: str
    details: list[ErrorDetailItem]
    warnings: list[WarningItem]
    correlation_id: str
    """Echoes ``X-Request-Id`` when the client sent one, otherwise a generated value."""


class ErrorResponse(ApiModel):
    error: ErrorBody
