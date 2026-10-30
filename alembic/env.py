"""Alembic environment for the EMS schema.

The database URL comes from application settings, not from ``alembic.ini``, so a migration
can never be applied to a different database than the one the application talks to.

``compare_type`` and ``compare_server_default`` are on, because this schema leans on exact
column types (``BINARY(16)`` keys, ``DECIMAL(15,2)`` money, ``DATETIME(6)`` timestamps) and a
silent type drift would break the §1.4 guarantees.

Autogenerate is a starting point, not the deliverable. §10 of the brief requires every
generated script to be reviewed by hand; in particular Alembic does not emit the MySQL
**stored generated columns** this schema uses to emulate PostgreSQL partial and exclusion
constraints (ARCHITECTURE §7), so those are written explicitly in the migration.
"""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# Allow `alembic` to run from the repository root without the package being installed.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db.base import Base  # noqa: E402
from app.models import import_all_models  # noqa: E402

# Importing every model module is what populates Base.metadata. Without this, autogenerate
# would cheerfully produce a migration that drops every table it cannot see.
import_all_models()

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def resolve_database_url() -> str:
    """The database to migrate.

    A URL already present on the Alembic config wins: that is how the test harness and a CI
    pipeline target a specific schema without the ambient ``.env`` being involved. Otherwise
    it comes from application settings, so an operator running ``alembic upgrade head`` by
    hand cannot migrate a different database than the application talks to.

    ``alembic.ini`` deliberately carries no URL, so there is no third source to disagree.
    """
    if (supplied := config.get_main_option("sqlalchemy.url")):
        return supplied

    from app.core.config import get_settings

    return get_settings().database_url


database_url = resolve_database_url()
config.set_main_option("sqlalchemy.url", database_url)

target_metadata = Base.metadata


def include_object(
    obj: object, name: str | None, type_: str, reflected: bool, compare_to: object
) -> bool:
    """Keep autogenerate focused on the EMS schema.

    ``alembic_version`` is Alembic's own bookkeeping table and must never appear in a
    migration.
    """
    return not (type_ == "table" and name == "alembic_version")


def run_migrations_offline() -> None:
    """Emit SQL without a connection (``alembic upgrade --sql``).

    Used to produce a reviewable script for a change-controlled production deployment.
    """
    context.configure(
        url=database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Apply migrations against a live connection."""
    section = config.get_section(config.config_ini_section, {})
    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
            include_object=include_object,
            # MySQL DDL is not transactional, so a failed migration leaves the schema
            # partially changed. Each revision is therefore written to be independently
            # reviewable and re-runnable; see docs/DATABASE.md.
            transaction_per_migration=True,
        )
        with context.begin_transaction():
            context.run_migrations()

    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
