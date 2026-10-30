"""Post-process the generated OpenAPI document so every error response is declared (BE-C01)."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI

from app.schemas.errors import ErrorBody, ErrorDetailItem, ErrorResponse, WarningItem

_HTTP_METHODS = {"get", "put", "post", "patch", "delete"}

ERROR_STATUS_DESCRIPTIONS: dict[str, str] = {
    "400": "Malformed request.",
    "401": "Missing, expired or invalid credentials.",
    "403": "Authenticated but not permitted, or the organization is not accessible.",
    "404": "The resource does not exist in this organization.",
    "409": "Conflict with current state, or a concurrent modification.",
    "412": "If-Match did not match the current ETag.",
    "422": "Validation failed.",
    "428": "If-Match is required for this operation.",
    "429": "Rate limited; see Retry-After.",
}

_ERROR_REF = {
    "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ErrorResponse"}}}
}


_RETRY_AFTER_HEADER = {
    "Retry-After": {"description": "Seconds to wait.", "schema": {"type": "integer"}}
}


def _error_components() -> dict[str, Any]:
    components: dict[str, Any] = {}
    for model in (ErrorResponse, ErrorBody, ErrorDetailItem, WarningItem):
        schema = model.model_json_schema(ref_template="#/components/schemas/{model}")
        schema.pop("$defs", None)
        components[model.__name__] = schema
    return components


def declare_error_responses(app: FastAPI, api_prefix: str) -> None:
    """Install an ``openapi()`` that attaches ``ErrorResponse`` to every API operation.

    Operations keep any more specific description they already declare; only gaps are filled.
    Probes outside ``api_prefix`` are left alone because they never return the envelope's
    business errors.
    """

    def build() -> dict[str, Any]:
        if app.openapi_schema:
            return app.openapi_schema
        schema = FastAPI.openapi(app)
        schema.setdefault("components", {}).setdefault("schemas", {}).update(_error_components())
        for path, item in schema["paths"].items():
            if not path.startswith(api_prefix):
                continue
            for method, operation in item.items():
                if method not in _HTTP_METHODS:
                    continue
                responses = operation.setdefault("responses", {})
                # FastAPI's default 422 body is HTTPValidationError, which we never emit.
                if "HTTPValidationError" in str(responses.get("422")):
                    del responses["422"]
                for code, description in ERROR_STATUS_DESCRIPTIONS.items():
                    declared = responses.setdefault(code, {"description": description})
                    declared.update({k: v for k, v in _ERROR_REF.items() if k not in declared})
                responses["429"].setdefault(
                    "headers",
                    {
                        "Retry-After": {
                            "description": "Seconds to wait.",
                            "schema": {"type": "integer"},
                        }
                    },
                )
        app.openapi_schema = schema
        return schema

    app.openapi = build  # type: ignore[method-assign]
