"""Operational command line.

    python -m app.cli generate-keys     # fresh secrets for a new .env
    python -m app.cli check-db          # verify database connectivity and server settings
    python -m app.cli show-config       # print non-secret configuration
    python -m app.cli seed              # seed roles, permissions, states, validation config
    python -m app.cli bootstrap         # first organization + administrator, empty db only
    python -m app.cli seed-demo         # Indian demo organization with every module in use

Secrets are printed to stdout for the operator to place into a secrets store. No command
writes a key to disk, and ``show-config`` never prints a secret value (§25.9).
"""

from __future__ import annotations

import argparse
import base64
import sys
from typing import Any

from app.core.crypto import generate_master_key, generate_opaque_token


def generate_keys() -> int:
    """Print a fresh set of secrets for a new environment."""
    encryption_key = base64.b64encode(generate_master_key()).decode("ascii")
    blind_index_key = base64.b64encode(generate_master_key()).decode("ascii")
    # Independently generated, so the §23.7 requirement that these differ holds by
    # construction rather than by the operator remembering to vary them.
    print("# Generated secrets. Store them in a secrets manager, not in version control.")
    print(f"SECRET_KEY={generate_opaque_token(48)}")
    print(f"ENCRYPTION_MASTER_KEY={encryption_key}")
    print(f"BLIND_INDEX_MASTER_KEY={blind_index_key}")
    print("ENCRYPTION_KEY_VERSION=1")
    return 0


def check_db() -> int:
    """Verify connectivity and report the session settings the application depends on.

    Prints the server version and the effective isolation level, SQL mode and timezone, so a
    misconfigured server is visible before migrations run. Never prints the connection URL,
    which contains credentials.
    """
    from sqlalchemy import text

    from app.core.config import get_settings
    from app.db.session import create_db_engine

    settings = get_settings()
    engine = create_db_engine(settings)
    probes = {
        "server_version": "SELECT VERSION()",
        "transaction_isolation": "SELECT @@SESSION.transaction_isolation",
        "sql_mode": "SELECT @@SESSION.sql_mode",
        "time_zone": "SELECT @@SESSION.time_zone",
        "lock_wait_timeout": "SELECT @@SESSION.innodb_lock_wait_timeout",
        "current_database": "SELECT DATABASE()",
    }
    try:
        with engine.connect() as connection:
            results = {
                name: connection.execute(text(query)).scalar() for name, query in probes.items()
            }
    except Exception as exc:  # a CLI probe reports, it does not traceback at the operator
        print(f"FAILED to connect: {type(exc).__name__}", file=sys.stderr)
        print(
            "Check DATABASE_URL, that the server is running and that the schema exists.",
            file=sys.stderr,
        )
        return 1
    finally:
        engine.dispose()

    print("Connected.")
    for name, value in results.items():
        print(f"  {name:24} {value}")

    version = str(results["server_version"])
    if not version.startswith("8.") and not version.startswith("9."):
        print(f"\nWARNING: MySQL 8.0+ is required (ADR-002); server reports {version}.")
    if "STRICT_ALL_TABLES" not in str(results["sql_mode"]):
        print("\nWARNING: STRICT_ALL_TABLES is not active; values could be silently truncated.")
    if "READ-COMMITTED" not in str(results["transaction_isolation"]):
        print("\nWARNING: isolation is not READ-COMMITTED; §18.8 overlap checks rely on it.")
    return 0


def show_config() -> int:
    """Print non-secret configuration, with every secret replaced by a marker."""
    from pydantic import SecretStr

    from app.core.config import get_settings

    settings = get_settings()
    redacted = {"database_url"}
    for name in sorted(type(settings).model_fields):
        value: Any = getattr(settings, name)
        if isinstance(value, SecretStr) or name in redacted:
            value = "[set]" if value else "[unset]"
        print(f"{name:34} {value}")
    return 0


def seed() -> int:
    """Seed the platform reference data (§5 roles, §21.2 config, §23.2 states).

    Idempotent, so it is safe to run on every deployment. The platform seed itself creates no
    user; when ``SEED_ADMIN_EMAIL``, ``SEED_ADMIN_PASSWORD`` and ``ORGANIZATION_NAME`` are
    configured and the database holds no users and no organizations, :func:`bootstrap` then
    runs in a second transaction to create the first administrator.
    """
    from app.core.config import get_settings
    from app.db.session import get_session_factory, unit_of_work
    from app.services.seed_data import seed_platform

    session = get_session_factory()()
    try:
        with unit_of_work(session):
            report = seed_platform(session)
    finally:
        session.close()

    print("Platform seed complete.")
    print(f"  roles created            {report.roles}")
    print(f"  permissions created      {report.permissions}")
    print(f"  role-permission links    {report.role_permissions}")
    print(f"  states created           {report.states}")
    print(f"  identifier configs       {report.identifier_configs}")
    if report.total() == 0:
        print("\nNothing to do: the platform is already seeded.")

    if not get_settings().bootstrap_configured:
        print(
            "\nNo user was created. Create the first administrator explicitly, or set "
            "ORGANIZATION_NAME, SEED_ADMIN_EMAIL and SEED_ADMIN_PASSWORD and re-run."
        )
        return 0

    print()
    # A separate transaction: a bootstrap failure must not roll back the platform seed, since
    # the seed is the precondition for retrying the bootstrap.
    return bootstrap()


def bootstrap() -> int:
    """Create the first organization and administrator on an empty database.

    Reads ``ORGANIZATION_NAME``, ``SEED_ADMIN_EMAIL`` and ``SEED_ADMIN_PASSWORD``. A no-op
    once any user or any organization exists, so it is safe to run repeatedly.
    """
    from app.core.config import get_settings
    from app.core.errors import EmsError
    from app.db.session import get_session_factory, unit_of_work
    from app.services.bootstrap import bootstrap_first_admin

    settings = get_settings()
    if settings.environment.is_production_like:
        # Unreachable through a validated Settings (the production check rejects the
        # password outright), and stated here so the refusal is explicit at the command.
        print(
            f"Refusing to bootstrap in environment={settings.environment}: §19 forbids a "
            "known-password administrator outside development.",
            file=sys.stderr,
        )
        return 1
    email = settings.seed_admin_email
    password = settings.seed_admin_password
    organization_name = settings.organization_name
    if not (email and password and organization_name):
        print(
            "Set ORGANIZATION_NAME, SEED_ADMIN_EMAIL and SEED_ADMIN_PASSWORD before "
            "running bootstrap.",
            file=sys.stderr,
        )
        return 1

    session = get_session_factory()()
    try:
        with unit_of_work(session):
            report = bootstrap_first_admin(
                session,
                email=email,
                password=password.get_secret_value(),
                organization_name=organization_name,
            )
    except EmsError as exc:
        print(f"Bootstrap failed: {exc.message}", file=sys.stderr)
        return 1
    finally:
        session.close()

    if not report.performed:
        print(f"Bootstrap skipped: {report.skip_reason}.")
        return 0

    print("Bootstrap complete.")
    print(f"  organization             {report.organization_name} ({report.organization_code})")
    print(f"  administrator            {report.email}")
    print(f"  roles granted            {', '.join(report.role_codes)}")
    print("\nThe password is the configured SEED_ADMIN_PASSWORD; change it before use.")
    if report.address_is_placeholder:
        print(
            "The organization's registered address and entity type are placeholders. "
            "Correct them with PATCH /organizations/{id} before entering real data."
        )
    return 0


def seed_demo_command() -> int:
    """Load the demo organization (development only; also applied at startup by AUTO_SEED)."""
    from app.core.config import get_settings
    from app.core.errors import EmsError
    from app.demo.runner import run_auto_seed
    from app.demo.seeder import DemoSeedError
    from app.main import create_app

    settings = get_settings()
    if settings.environment.is_production_like:
        print("Refusing to load demo data outside development.", file=sys.stderr)
        return 1
    try:
        report = run_auto_seed(create_app(settings), settings)
    except (DemoSeedError, EmsError) as exc:
        print(f"Demo seed failed: {exc}", file=sys.stderr)
        return 1
    if not report.seeded:
        print(f"Demo seed skipped: {report.skip_reason}.")
        return 0
    print("Demo data loaded.")
    for key, value in sorted(report.counts.items()):
        print(f"  {key:24s} {value}")
    print(f"\nSign in with any of these (password: {report.password}):")
    for login in report.logins:
        print(f"  {login}")
    return 0


def run_jobs() -> int:
    """Run the §24.7 housekeeping jobs once for every active organization."""
    from sqlalchemy import select

    from app.core.config import get_settings
    from app.db.session import get_session_factory, unit_of_work
    from app.jobs.runner import run_attendance_jobs, run_for_organization, run_leave_jobs
    from app.models.organization import Organization
    from app.storage.local import LocalStorage

    settings = get_settings()
    storage = LocalStorage(settings.storage_local_root)
    session = get_session_factory()()
    totals: dict[str, int] = {}
    try:
        organizations = list(
            session.execute(
                select(Organization.id).where(Organization.status == "ACTIVE")
            ).scalars()
        )
        for organization_id in organizations:
            with unit_of_work(session):
                report = run_for_organization(session, storage, organization_id)
            # Leave jobs commit per employee themselves, so they run outside unit_of_work.
            report |= run_leave_jobs(session, organization_id)
            report |= run_attendance_jobs(session, organization_id)
            for key, value in report.items():
                totals[key] = totals.get(key, 0) + value
    finally:
        session.close()
    print("Scheduled jobs complete.")
    for key, value in sorted(totals.items()):
        print(f"  {key:24s} {value}")
    return 0


def serve_jobs() -> int:
    """Run the §24.7 jobs every JOB_INTERVAL_SECONDS until interrupted."""
    import time

    from app.core.config import get_settings

    interval = get_settings().job_interval_seconds
    print(f"Job scheduler started; interval {interval}s. Press Ctrl+C to stop.")
    try:
        while True:
            run_jobs()
            time.sleep(interval)
    except KeyboardInterrupt:
        print("Job scheduler stopped.")
        return 0


COMMANDS = {
    "generate-keys": generate_keys,
    "check-db": check_db,
    "show-config": show_config,
    "seed": seed,
    "bootstrap": bootstrap,
    "seed-demo": seed_demo_command,
    "run-jobs": run_jobs,
    "serve-jobs": serve_jobs,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description=__doc__)
    parser.add_argument("command", choices=sorted(COMMANDS))
    arguments = parser.parse_args(argv)
    return COMMANDS[arguments.command]()


if __name__ == "__main__":
    raise SystemExit(main())
