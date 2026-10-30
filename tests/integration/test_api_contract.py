"""Application-level contract: error envelope, security headers, probes, OpenAPI.

These exercise the HTTP layer only and need no database, so they run everywhere.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from fastapi import APIRouter

from app.api.middleware import (
    DOCS_CONTENT_SECURITY_POLICY,
    DOCS_UI_PATHS,
    NO_STORE_CACHE_CONTROL,
    SECURITY_HEADERS,
)
from app.core.errors import (
    ErrorCode,
    OrgAccessDenied,
    PermissionDenied,
    PreconditionFailed,
    RateLimited,
    ResourceNotFound,
    SalaryAssignmentOverlap,
    Unauthenticated,
    ValidationFailed,
)
from app.core.logging import REQUEST_ID_HEADER


class TestProbes:
    def test_health_reports_ok_and_nothing_else(self, unauthenticated_client: Any) -> None:
        response = unauthenticated_client.get("/health")
        assert response.status_code == 200
        # §18 of the brief: probes expose no secrets. An exact-equality assertion is what
        # stops a version, hostname or configuration value being added later.
        assert response.json() == {"status": "ok"}

    def test_health_does_not_touch_the_database(self, unauthenticated_client: Any) -> None:
        """Liveness must not fail because a dependency is down."""
        assert unauthenticated_client.get("/health").status_code == 200

    def test_readiness_reports_database_state(self, unauthenticated_client: Any) -> None:
        response = unauthenticated_client.get("/ready")
        assert response.status_code == 200
        body = response.json()
        assert set(body) == {"status", "database"}
        assert body["status"] in {"ready", "degraded"}
        assert body["database"] in {"up", "down"}

    def test_readiness_leaks_no_connection_detail(self, unauthenticated_client: Any) -> None:
        payload = unauthenticated_client.get("/ready").text
        for leak in ("mysql", "pymysql", "127.0.0.1", "3306", "password", "root"):
            assert leak not in payload.lower()


class TestSecurityHeaders:
    @pytest.mark.parametrize("header", sorted(SECURITY_HEADERS))
    def test_header_present(self, unauthenticated_client: Any, header: str) -> None:
        response = unauthenticated_client.get("/health")
        assert response.headers.get(header) == SECURITY_HEADERS[header]

    def test_hsts_is_long_lived(self, unauthenticated_client: Any) -> None:
        """NFR-11: HSTS enabled."""
        value = unauthenticated_client.get("/health").headers["Strict-Transport-Security"]
        assert "max-age=31536000" in value

    def test_headers_are_present_on_error_responses_too(self, unauthenticated_client: Any) -> None:
        response = unauthenticated_client.get("/does-not-exist")
        assert response.status_code == 404
        assert response.headers["X-Content-Type-Options"] == "nosniff"


class TestInteractiveDocs:
    """The OpenAPI UI pages, which need a policy the JSON API does not."""

    @pytest.mark.parametrize("path", sorted(DOCS_UI_PATHS))
    def test_ui_page_carries_the_relaxed_policy(
        self, unauthenticated_client: Any, path: str
    ) -> None:
        response = unauthenticated_client.get(path)
        assert response.status_code == 200
        assert response.headers["Content-Security-Policy"] == DOCS_CONTENT_SECURITY_POLICY

    @pytest.mark.parametrize("path", ["/docs", "/redoc"])
    def test_every_asset_the_page_references_is_permitted(
        self, unauthenticated_client: Any, path: str
    ) -> None:
        """Regression: under `default-src 'none'` the page returned 200 but the browser
        blocked `swagger-ui-bundle.js`, so the UI rendered as a blank frame."""
        response = unauthenticated_client.get(path)
        policy = response.headers["Content-Security-Policy"]
        origins = {
            match.group(0)
            for match in re.finditer(r"https://[a-z0-9.\-]+", response.text, re.IGNORECASE)
        }
        assert origins, "the page references no assets -- the extraction is wrong, not the policy"
        for origin in origins:
            assert origin in policy, f"{path} loads {origin}, which its CSP does not allow"

    def test_policy_still_denies_by_default(self) -> None:
        """Only the directives the bundled UI needs are opened up."""
        assert DOCS_CONTENT_SECURITY_POLICY.startswith("default-src 'none'")
        assert "frame-ancestors 'none'" in DOCS_CONTENT_SECURITY_POLICY
        assert "base-uri 'none'" in DOCS_CONTENT_SECURITY_POLICY
        assert "unsafe-eval" not in DOCS_CONTENT_SECURITY_POLICY

    @pytest.mark.parametrize("path", ["/health", "/ready", "/openapi.json", "/does-not-exist"])
    def test_everything_else_keeps_the_strict_policy(
        self, unauthenticated_client: Any, path: str
    ) -> None:
        """The relaxation is scoped to three HTML pages; no data path inherits it."""
        response = unauthenticated_client.get(path)
        assert (
            response.headers["Content-Security-Policy"]
            == SECURITY_HEADERS["Content-Security-Policy"]
        )

    def test_other_security_headers_are_unaffected(self, unauthenticated_client: Any) -> None:
        headers = unauthenticated_client.get("/docs").headers
        for header, value in SECURITY_HEADERS.items():
            if header == "Content-Security-Policy":
                continue
            assert headers.get(header) == value


class TestCorrelationId:
    def test_response_carries_a_request_id(self, unauthenticated_client: Any) -> None:
        response = unauthenticated_client.get("/health")
        assert response.headers[REQUEST_ID_HEADER]

    def test_client_supplied_id_is_echoed(self, unauthenticated_client: Any) -> None:
        response = unauthenticated_client.get(
            "/health", headers={REQUEST_ID_HEADER: "trace-abc-123"}
        )
        assert response.headers[REQUEST_ID_HEADER] == "trace-abc-123"

    @pytest.mark.parametrize(
        "hostile",
        [
            "a" * 200,
            "has spaces",
            "new\nline",
            "semi;colon",
            "<script>",
        ],
    )
    def test_hostile_request_id_is_replaced(
        self, unauthenticated_client: Any, hostile: str
    ) -> None:
        """The id is echoed and stored in audit rows, so it must not be a log-injection path."""
        response = unauthenticated_client.get("/health", headers={REQUEST_ID_HEADER: hostile})
        returned = response.headers[REQUEST_ID_HEADER]
        assert returned != hostile
        assert returned.isalnum()

    def test_ids_differ_between_requests(self, unauthenticated_client: Any) -> None:
        first = unauthenticated_client.get("/health").headers[REQUEST_ID_HEADER]
        second = unauthenticated_client.get("/health").headers[REQUEST_ID_HEADER]
        assert first != second


class TestErrorEnvelope:
    """§24.2: every error has the same shape."""

    def test_unknown_path_uses_the_envelope(self, unauthenticated_client: Any) -> None:
        response = unauthenticated_client.get("/api/v1/nope")
        assert response.status_code == 404
        error = response.json()["error"]
        assert error["code"] == ErrorCode.RESOURCE_NOT_FOUND.value
        assert set(error) == {"code", "message", "details", "warnings", "correlation_id"}

    def test_envelope_correlation_id_matches_the_header(self, unauthenticated_client: Any) -> None:
        response = unauthenticated_client.get(
            "/api/v1/nope", headers={REQUEST_ID_HEADER: "trace-xyz"}
        )
        assert response.json()["error"]["correlation_id"] == "trace-xyz"

    def test_method_not_allowed_uses_the_envelope(self, unauthenticated_client: Any) -> None:
        response = unauthenticated_client.post("/health")
        assert response.status_code == 405
        assert response.json()["error"]["code"] == ErrorCode.MALFORMED_REQUEST.value


class TestDomainErrorRendering:
    """Each spec error class must render with its documented status and code."""

    CASES = (
        (Unauthenticated, 401, ErrorCode.UNAUTHENTICATED),
        (PermissionDenied, 403, ErrorCode.PERMISSION_DENIED),
        (OrgAccessDenied, 403, ErrorCode.ORG_ACCESS_DENIED),
        (ResourceNotFound, 404, ErrorCode.RESOURCE_NOT_FOUND),
        (SalaryAssignmentOverlap, 409, ErrorCode.SALARY_ASSIGNMENT_OVERLAP),
        (PreconditionFailed, 412, ErrorCode.PRECONDITION_FAILED),
        (ValidationFailed, 422, ErrorCode.VALIDATION_FAILED),
        (RateLimited, 429, ErrorCode.RATE_LIMITED),
    )

    @pytest.fixture(scope="class")
    @staticmethod
    def erroring_client(isolated_app: Any) -> Any:
        """Mount a throwaway router that raises each error on demand."""
        from fastapi.testclient import TestClient

        router = APIRouter()
        lookup = {error.__name__: error for error, _, _ in TestDomainErrorRendering.CASES}

        @router.get("/_test/raise/{name}")
        def raise_error(name: str) -> None:
            raise lookup[name]()

        isolated_app.include_router(router)
        with TestClient(isolated_app, raise_server_exceptions=False) as client:
            yield client

    @pytest.mark.parametrize(("error", "status", "code"), CASES)
    def test_renders_with_spec_status_and_code(
        self, erroring_client: Any, error: type, status: int, code: ErrorCode
    ) -> None:
        response = erroring_client.get(f"/_test/raise/{error.__name__}")
        assert response.status_code == status
        assert response.json()["error"]["code"] == code.value

    def test_unauthenticated_sets_www_authenticate(self, erroring_client: Any) -> None:
        response = erroring_client.get("/_test/raise/Unauthenticated")
        assert response.headers["WWW-Authenticate"] == "Bearer"


class TestUnhandledExceptions:
    """§8 of the brief and OWASP A10: never leak internals."""

    @pytest.fixture(scope="class")
    @staticmethod
    def crashing_client(isolated_app: Any) -> Any:
        from fastapi.testclient import TestClient

        router = APIRouter()

        @router.get("/_test/crash")
        def crash() -> None:
            raise RuntimeError(
                "connection to mysql://root:hunter2@10.0.0.5/ems failed at /srv/app/db.py:42"
            )

        isolated_app.include_router(router)
        with TestClient(isolated_app, raise_server_exceptions=False) as client:
            yield client

    def test_returns_opaque_500(self, crashing_client: Any) -> None:
        response = crashing_client.get("/_test/crash")
        assert response.status_code == 500
        assert response.json()["error"]["code"] == ErrorCode.INTERNAL_ERROR.value

    def test_response_contains_no_internal_detail(self, crashing_client: Any) -> None:
        body = crashing_client.get("/_test/crash").text
        for leak in ("RuntimeError", "Traceback", "hunter2", "10.0.0.5", "db.py", "/srv/app"):
            assert leak not in body

    def test_correlation_id_is_still_returned(self, crashing_client: Any) -> None:
        """The only thing a client gets is the id needed to find the logged detail."""
        response = crashing_client.get("/_test/crash")
        assert response.json()["error"]["correlation_id"]


class TestCachePolicy:
    def test_api_responses_are_not_cacheable(self, isolated_app: Any) -> None:
        """Every endpoint returns tenant-scoped personal data (§11.6, §15.6)."""
        from fastapi import APIRouter
        from fastapi.testclient import TestClient

        router = APIRouter(prefix="/api/v1")

        @router.get("/_test/data")
        def data() -> dict[str, str]:
            return {"value": "x"}

        isolated_app.include_router(router)
        with TestClient(isolated_app) as client:
            response = client.get("/api/v1/_test/data")
        assert response.headers["Cache-Control"] == NO_STORE_CACHE_CONTROL


class TestOpenApi:
    def test_document_is_generated(self, unauthenticated_client: Any) -> None:
        response = unauthenticated_client.get("/openapi.json")
        assert response.status_code == 200
        assert response.json()["info"]["title"] == "Employee Management System API"

    def test_description_states_the_scope_boundary(self, unauthenticated_client: Any) -> None:
        """AC-24: the published contract must not imply HRMS capability."""
        description = unauthenticated_client.get("/openapi.json").json()["info"]["description"]
        assert "not an HRMS" in description
        assert "no payroll" in description.lower()
