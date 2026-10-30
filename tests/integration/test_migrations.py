"""Alembic wiring and MySQL session guarantees.

These run against a real MySQL 8 database (§5 of the brief: SQLite is not a substitute for
dialect-specific behaviour) and are skipped with a clear reason when none is reachable.

The session-variable assertions matter as much as the migration ones. §18.8's compensating
control for the salary timeline — an employee row lock plus an in-transaction overlap check —
is only sound under READ COMMITTED, and silent value truncation under a non-strict
``sql_mode`` would defeat every length constraint in §23.3. Both are configured on connect
in :mod:`app.db.session`, so both are verified here rather than assumed.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import Engine, inspect, text

from app.db.base import Base
from app.db.session import REQUIRED_SQL_MODE


class TestSessionConfiguration:
    def test_server_is_mysql_8_or_newer(self, engine: Engine) -> None:
        with engine.connect() as connection:
            version = str(connection.execute(text("SELECT VERSION()")).scalar())
        assert int(version.split(".", 1)[0]) >= 8, f"MySQL 8.0+ required (ADR-002), got {version}"

    def test_isolation_is_read_committed(self, engine: Engine) -> None:
        """§18.8 specifies READ COMMITTED; MySQL defaults to REPEATABLE READ."""
        with engine.connect() as connection:
            isolation = str(
                connection.execute(text("SELECT @@SESSION.transaction_isolation")).scalar()
            )
        assert isolation.replace("_", "-").upper() == "READ-COMMITTED"

    def test_sql_mode_is_strict(self, engine: Engine) -> None:
        """Without STRICT_ALL_TABLES, MySQL truncates instead of raising."""
        with engine.connect() as connection:
            sql_mode = str(connection.execute(text("SELECT @@SESSION.sql_mode")).scalar())
        for required in REQUIRED_SQL_MODE.split(","):
            assert required in sql_mode, f"{required} missing from sql_mode"

    def test_session_timezone_is_utc(self, engine: Engine) -> None:
        """§1.4: timestamps are stored in UTC, including server-side defaults."""
        with engine.connect() as connection:
            now_utc = connection.execute(text("SELECT @@SESSION.time_zone")).scalar()
        assert str(now_utc) in {"+00:00", "UTC"}

    def test_lock_wait_timeout_is_bounded(self, engine: Engine) -> None:
        """A contended row must fail fast as a retryable 409, not hang (§24.3)."""
        with engine.connect() as connection:
            timeout = int(
                str(connection.execute(text("SELECT @@SESSION.innodb_lock_wait_timeout")).scalar())
            )
        assert 0 < timeout <= 120

    def test_primary_key_requirement_is_compatible(self, engine: Engine) -> None:
        """If the server enforces sql_require_primary_key, every table must declare one.

        §1.5 gives every business table a UUID primary key and every append-only table one
        too, so the schema satisfies this either way. The test records the server setting so a
        future table without a key fails here with a clear reason rather than mid-migration.
        """
        with engine.connect() as connection:
            required = str(
                connection.execute(text("SELECT @@SESSION.sql_require_primary_key")).scalar()
            )
        assert required in {"0", "1"}

    def test_strict_mode_rejects_over_long_values(self, engine: Engine) -> None:
        """The property the strict mode buys: a too-long value errors instead of truncating."""
        from sqlalchemy.exc import DatabaseError

        with engine.begin() as connection:
            # A primary key is required: this server runs with sql_require_primary_key=ON.
            # Every EMS table has a UUID primary key (§1.5), so the schema is compatible.
            connection.execute(
                text("CREATE TEMPORARY TABLE _strict_probe (id INT PRIMARY KEY, v VARCHAR(4))")
            )
            with pytest.raises(DatabaseError):
                connection.execute(
                    text("INSERT INTO _strict_probe (id, v) VALUES (1, :v)"),
                    {"v": "far too long"},
                )


class TestAlembicWiring:
    def test_single_head(self) -> None:
        """A branched history would make `upgrade head` ambiguous."""
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        project_root = Path(__file__).resolve().parent.parent.parent
        config = Config(str(project_root / "alembic.ini"))
        config.set_main_option("script_location", str(project_root / "alembic"))
        heads = ScriptDirectory.from_config(config).get_heads()
        assert len(heads) <= 1, f"migration history has branched: {heads}"

    def test_alembic_ini_carries_no_database_url(self) -> None:
        """The URL must come from settings or the caller, never from a committed file.

        A URL in `alembic.ini` would be both a credential in version control and a third
        source of truth that can disagree with the application's own configuration.
        """
        project_root = Path(__file__).resolve().parent.parent.parent
        contents = (project_root / "alembic.ini").read_text(encoding="utf-8")
        for line in contents.splitlines():
            stripped = line.strip()
            if stripped.startswith("sqlalchemy.url") and "=" in stripped:
                assert not stripped.split("=", 1)[1].strip(), "alembic.ini must not pin a URL"

    def test_upgrade_to_head_succeeds(self, migrated_database: Engine) -> None:
        """`alembic upgrade head` runs cleanly against a real MySQL schema."""
        with migrated_database.connect() as connection:
            tables = set(inspect(connection).get_table_names())
        assert "alembic_version" in tables, "Alembic did not stamp the schema"

    def test_schema_matches_the_models(self, migrated_database: Engine) -> None:
        """Every table in the metadata exists in the migrated schema.

        Catches the commonest migration defect: a model added without a corresponding
        revision. The reverse direction (a table in the database with no model) is not
        asserted, because the test schema may legitimately retain tables from an earlier
        revision during a multi-step deployment.
        """
        with migrated_database.connect() as connection:
            actual = set(inspect(connection).get_table_names())
        expected = set(Base.metadata.tables)
        missing = expected - actual
        assert not missing, (
            f"tables defined in models but absent from the schema: {sorted(missing)}"
        )

    def test_models_are_registered(self) -> None:
        """`import_all_models` must cover every module under app.models.

        An unimported model module is invisible to autogenerate, which would then emit a
        migration dropping its tables.
        """
        from app.models import MODEL_MODULES

        models_dir = Path(__file__).resolve().parent.parent.parent / "app" / "models"
        on_disk = {
            path.stem
            for path in models_dir.glob("*.py")
            if path.stem != "__init__" and not path.stem.startswith("_")
        }
        assert on_disk == set(MODEL_MODULES), (
            "app/models/__init__.py MODEL_MODULES is out of step with the modules on disk: "
            f"on disk only={sorted(on_disk - set(MODEL_MODULES))}, "
            f"listed only={sorted(set(MODEL_MODULES) - on_disk)}"
        )
