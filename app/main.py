"""FastAPI application entrypoint."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api.router import api_router
from app.core.config import settings
from app.core.database import SessionLocal, check_connection, init_db
from app.core.errors import register_exception_handlers
from app.core.middleware import IdempotencyMiddleware


def _maybe_auto_seed() -> None:
    if not settings.auto_seed:
        return
    from app.seed import database_is_empty, run_seed

    db = SessionLocal()
    try:
        empty = database_is_empty(db)
    finally:
        db.close()
    if empty:
        summary = run_seed()
        print(f"Auto-seed complete: {summary}")


@asynccontextmanager
async def lifespan(_: FastAPI):
    # For local/dev and tests we create tables if missing. Production should use
    # Alembic migrations instead.
    init_db()
    _maybe_auto_seed()
    yield


app = FastAPI(
    title=settings.app_name,
    version=__version__,
    description="Keka-style HR & Payroll suite REST API.",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins or ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

register_exception_handlers(app)
app.add_middleware(IdempotencyMiddleware)
app.include_router(api_router, prefix=settings.api_prefix)


@app.get("/", tags=["Meta"], summary="Service root")
def root() -> dict:
    return {
        "data": {
            "name": settings.app_name,
            "version": __version__,
            "docs": "/docs",
            "api_prefix": settings.api_prefix,
        },
        "meta": None,
        "errors": None,
    }


@app.get("/health", tags=["Meta"], summary="Health check")
def health() -> dict:
    return {
        "data": {"status": "ok", "database": "up" if check_connection() else "down"},
        "meta": None,
        "errors": None,
    }
