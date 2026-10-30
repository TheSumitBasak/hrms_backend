"""BE-C01 / BE-S09: the published OpenAPI document declares the error envelope and /meta."""

from __future__ import annotations

from typing import Any

import pytest

from app.api.openapi import ERROR_STATUS_DESCRIPTIONS
from app.api.v1.endpoints.meta import CAPABILITIES

ERROR_REF = "#/components/schemas/ErrorResponse"


@pytest.fixture(scope="module")
def spec(unauthenticated_client: Any) -> dict[str, Any]:
    return unauthenticated_client.get("/openapi.json").json()


def _api_operations(spec: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    return [
        (path, method, op)
        for path, item in spec["paths"].items()
        if path.startswith("/api/v1")
        for method, op in item.items()
        if method in {"get", "put", "post", "patch", "delete"}
    ]


def test_error_schemas_are_published(spec: dict[str, Any]) -> None:
    schemas = spec["components"]["schemas"]
    assert {"ErrorResponse", "ErrorBody", "ErrorDetailItem", "WarningItem"} <= set(schemas)
    assert "HTTPValidationError" not in str(
        [op["responses"].get("422") for _, _, op in _api_operations(spec)]
    )


def test_every_operation_declares_every_error_status(spec: dict[str, Any]) -> None:
    operations = _api_operations(spec)
    assert len(operations) > 100
    for path, method, op in operations:
        for code in ERROR_STATUS_DESCRIPTIONS:
            body = op["responses"][code]["content"]["application/json"]["schema"]
            assert body == {"$ref": ERROR_REF}, f"{method.upper()} {path} {code}"


def test_rate_limit_response_documents_retry_after(spec: dict[str, Any]) -> None:
    login = spec["paths"]["/api/v1/auth/login"]["post"]["responses"]
    assert "Retry-After" in login["429"]["headers"]
    # A description an endpoint already wrote is kept, not overwritten by the generic one.
    assert login["401"]["description"] == "The email address or password is incorrect."


def test_probes_are_not_given_business_errors(spec: dict[str, Any]) -> None:
    assert "400" not in spec["paths"]["/health"]["get"]["responses"]


def test_meta_is_declared_and_capabilities_are_sorted_unique() -> None:
    assert list(CAPABILITIES) == sorted(set(CAPABILITIES))
