"""Application factory and entrypoint.

Composition root: this is the only module that knows how to assemble settings, logging,
middleware, exception handlers and routers. Everything else receives what it needs by
injection, which is what keeps services testable without an HTTP layer.

Routers are mounted from :mod:`app.api.router` as each domain module lands; see
``docs/IMPLEMENTATION_CHECKLIST.md`` for current coverage.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.exc import SQLAlchemyError

from app import __version__
from app.api.middleware import RequestContextMiddleware, SecurityHeadersMiddleware
from app.api.openapi import declare_error_responses
from app.api.router import api_router
from app.core.config import Settings, get_settings
from app.core.errors import EmsError
from app.core.exception_handlers import register_exception_handlers
from app.core.logging import configure_logging
from app.db.session import check_connection, configure_engine, create_db_engine, dispose_engine
from app.demo import runner
from app.demo.seeder import DemoSeedError
from app.schemas.common import HealthResponse, ReadinessResponse

logger = logging.getLogger(__name__)

DESCRIPTION = """
System of record for employee information across multiple legally distinct organizations
in India.

**This is not an HRMS.** It performs no payroll, tax, statutory contribution, attendance,
leave, recruitment, performance, training, expense, asset or settlement processing, and
computes no pay totals, deductions or derived compensation figures. Salary structures,
statutory identifiers and dates are stored as reference data only.

Conventions: organization-scoped resources live under `/orgs/{org_id}`; single-resource GETs
return an `ETag` that mutating requests must echo in `If-Match`; create endpoints for
employees, salary assignments and documents require an `Idempotency-Key`; lists use signed
cursor pagination. Errors use a single envelope with a stable machine-readable code.
"""


async def _auto_seed(app: FastAPI, config: Settings) -> None:
    """Load the demo organization (``AUTO_SEED=true``). A failure is logged, not fatal."""
    try:
        report = await asyncio.to_thread(runner.run_auto_seed, app, config)
    except (DemoSeedError, EmsError, SQLAlchemyError):
        logger.exception("AUTO_SEED failed; the application continues without demo data")
        return
    logger.info(
        "AUTO_SEED finished",
        extra={"seeded": report.seeded, "skip_reason": report.skip_reason, **report.counts},
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the ASGI application.

    Accepts explicit settings so the test harness can construct an app against a test
    database without mutating the process environment.
    """
    config = settings or get_settings()
    configure_logging(level=config.log_level, log_format=config.log_format, sql_echo=config.db_echo)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        """Startup and shutdown.

        Deliberately does **not** create or migrate the schema. §10 of the brief forbids
        destructive automatic migrations, and creating tables implicitly would let a
        deployment drift from its Alembic history. Schema changes are an explicit
        `alembic upgrade head` step (see docs/DATABASE.md).
        """
        # Bind the process database handle to *this* application's settings, so an app
        # built for a test or a job never talks to the database named in the ambient .env.
        configure_engine(create_db_engine(config))
        logger.info(
            "application starting",
            extra={
                "environment": config.environment.value,
                "version": __version__,
                "oidc_enabled": config.oidc_enabled,
                "storage_backend": config.storage_backend,
            },
        )
        if not check_connection():
            # A failing probe is logged, not fatal: the readiness endpoint reports it and the
            # orchestrator decides. Crashing on a transient database blip would turn a
            # recoverable condition into an outage.
            logger.error("database is not reachable at startup")
        elif config.auto_seed:
            await _auto_seed(app, config)
        yield
        dispose_engine()
        logger.info("application stopped")

    app = FastAPI(
        title="Employee Management System API",
        version=__version__,
        description=DESCRIPTION,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        # Our handlers own the envelope; FastAPI's default 422 shape would bypass §24.2.
        responses={},
    )

    # Dependencies resolve settings from here, so an app built with explicit settings is
    # self-contained and never reads the ambient environment mid-request.
    app.state.settings = config

    if config.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=config.cors_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE"],
            allow_headers=[
                "Authorization",
                "Content-Type",
                "If-Match",
                "Idempotency-Key",
                "X-Request-Id",
            ],
            expose_headers=["ETag", "X-Request-Id"],
            max_age=600,
        )

    # Outermost first: correlation context must be bound before anything else logs, and
    # security headers must be applied to every response including error responses.
    app.add_middleware(SecurityHeadersMiddleware, api_prefix=config.api_prefix)
    app.add_middleware(RequestContextMiddleware, api_prefix=config.api_prefix)

    register_exception_handlers(app)
    declare_error_responses(app, config.api_prefix)
    _register_operational_routes(app)
    app.include_router(api_router, prefix=config.api_prefix)
    return app


def _register_operational_routes(app: FastAPI) -> None:
    """Liveness and readiness probes (§18 of the brief: no secrets exposed)."""

    @app.get(
        "/health",
        tags=["Operations"],
        summary="Liveness probe",
        response_model=HealthResponse,
    )
    def health() -> HealthResponse:
        """Report that the process is running. Performs no I/O and reveals no configuration."""
        return HealthResponse()

    @app.get(
        "/ready",
        tags=["Operations"],
        summary="Readiness probe",
        response_model=ReadinessResponse,
    )
    def ready() -> ReadinessResponse:
        """Report whether the database is reachable.

        Returns 200 with ``status: degraded`` rather than a 5xx, so a load balancer sees a
        definite answer and the body says which dependency is at fault — without disclosing
        a host, version or connection string.
        """
        database_up = check_connection()
        return ReadinessResponse(
            status="ready" if database_up else "degraded",
            database="up" if database_up else "down",
        )


# No module-level `app` instance. Building the application at import time would make
# importing anything from this module read and validate the environment as a side effect,
# which breaks test collection and job entrypoints that supply their own settings. Run the
# server against the factory instead:
#
#     uvicorn app.main:create_app --factory
#
# See docs/DEPLOYMENT.md for the production command.
