"""Custom application exceptions and handlers returning the standard envelope."""
from __future__ import annotations

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

STATUS_CODE_MAP: dict[int, str] = {
    status.HTTP_400_BAD_REQUEST: "VALIDATION",
    status.HTTP_401_UNAUTHORIZED: "UNAUTHENTICATED",
    status.HTTP_403_FORBIDDEN: "FORBIDDEN",
    status.HTTP_404_NOT_FOUND: "NOT_FOUND",
    status.HTTP_409_CONFLICT: "CONFLICT",
    422: "BUSINESS_RULE",
    status.HTTP_429_TOO_MANY_REQUESTS: "RATE_LIMITED",
    status.HTTP_500_INTERNAL_SERVER_ERROR: "INTERNAL",
}


class APIError(Exception):
    """Raise anywhere to return the standard error envelope."""

    def __init__(
        self,
        status_code: int,
        message: str,
        code: str | None = None,
        errors: list[dict] | None = None,
    ) -> None:
        self.status_code = status_code
        self.message = message
        self.code = code or STATUS_CODE_MAP.get(status_code, "ERROR")
        self.errors = errors or [{"code": self.code, "message": message}]
        super().__init__(message)


class NotFoundError(APIError):
    def __init__(self, entity: str = "Resource", entity_id: object | None = None) -> None:
        detail = f"{entity} not found"
        if entity_id is not None:
            detail = f"{entity} '{entity_id}' not found"
        super().__init__(status.HTTP_404_NOT_FOUND, detail, "NOT_FOUND")


class ConflictError(APIError):
    def __init__(self, message: str) -> None:
        super().__init__(status.HTTP_409_CONFLICT, message, "CONFLICT")


class BusinessRuleError(APIError):
    def __init__(self, message: str, code: str = "BUSINESS_RULE") -> None:
        super().__init__(422, message, code)


class PermissionDeniedError(APIError):
    def __init__(self, message: str = "You do not have permission to perform this action") -> None:
        super().__init__(status.HTTP_403_FORBIDDEN, message, "FORBIDDEN")


def _envelope(errors: list[dict]) -> dict:
    return {"data": None, "meta": None, "errors": errors}


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(APIError)
    async def _api_error(_: Request, exc: APIError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=_envelope(exc.errors))

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        errors = []
        for err in exc.errors():
            loc = ".".join(str(part) for part in err.get("loc", []) if part not in ("body", "query"))
            errors.append(
                {"code": "VALIDATION", "field": loc, "message": err.get("msg", "Invalid value")}
            )
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content=_envelope(errors or [{"code": "VALIDATION", "message": "Invalid request"}]),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = STATUS_CODE_MAP.get(exc.status_code, "ERROR")
        message = exc.detail if isinstance(exc.detail, str) else "Request failed"
        return JSONResponse(
            status_code=exc.status_code,
            content=_envelope([{"code": code, "message": message}]),
        )

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:  # pragma: no cover
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=_envelope([{"code": "INTERNAL", "message": "Internal server error"}]),
        )
