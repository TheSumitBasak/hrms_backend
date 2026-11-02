"""Startup / CLI entry point for demo seeding: platform seed, optional bootstrap, demo data."""

from __future__ import annotations

from typing import Any

from app.core.config import Settings
from app.db.session import get_session_factory, unit_of_work
from app.demo.seeder import DemoSeedReport, seed_demo
from app.services.bootstrap import bootstrap_first_admin
from app.services.seed_data import seed_platform


def run_auto_seed(app: Any, settings: Settings) -> DemoSeedReport:
    """Seed reference data, the configured administrator (if any) and the demo organization.

    Every step is a no-op when already done, so this is safe on every start. Blocking: call it
    from a worker thread inside an event loop.
    """
    session = get_session_factory()()
    try:
        with unit_of_work(session):
            seed_platform(session)
        if settings.bootstrap_configured and settings.seed_admin_password is not None:
            with unit_of_work(session):
                bootstrap_first_admin(
                    session,
                    email=settings.seed_admin_email or "",
                    password=settings.seed_admin_password.get_secret_value(),
                    organization_name=settings.organization_name or "",
                )
    finally:
        session.close()

    password = (
        settings.seed_admin_password.get_secret_value() if settings.seed_admin_password else None
    )
    return seed_demo(app, password=password, also_grant_email=settings.seed_admin_email)
