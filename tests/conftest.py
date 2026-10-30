"""Test harness.

Three principles:

**The suite never reads or mutates the developer's secrets.** Test settings are built in
memory with freshly generated throwaway keys. The only thing taken from the environment is a
database URL, and even that is derived rather than reused: the schema name is forced to end
in ``_test`` so a misconfiguration cannot point the destructive parts of the suite at a
development database.

**Integration tests require a real MySQL 8.** §5 of the brief forbids substituting SQLite for
dialect-specific behaviour, and this schema depends on MySQL stored generated columns, row
locks and ``READ-COMMITTED`` isolation. Tests needing a database are skipped with a clear
reason when none is reachable, and they are never silently redirected to another engine.

**No fixture contains realistic personal data.** §9 of the brief and §25.2 rule out
real-looking sensitive values in fixtures; identifiers here are structurally valid but
fictitious, matching §1.4.
"""

from __future__ import annotations

import base64
import os
import re
import secrets
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine, text

from app.core.config import Environment, Settings

TEST_SCHEMA_SUFFIX = "_test"
SKIP_NO_DATABASE = (
    "No MySQL 8 test database is reachable. Set EMS_TEST_DATABASE_URL (or DATABASE_URL, "
    "whose schema will be suffixed with '_test') and run: python scripts/create_databases.py"
)

_URL_WITH_SCHEMA = re.compile(
    r"^(?P<prefix>mysql\+pymysql://[^/]+/)(?P<schema>[^?]+)(?P<query>\?.*)?$"
)


# --------------------------------------------------------------------------- settings


def _generated_key() -> str:
    """A throwaway base64 32-byte key. Never a real key, never persisted."""
    return base64.b64encode(secrets.token_bytes(32)).decode("ascii")


def _resolve_test_database_url() -> str | None:
    """Find a test database URL, forcing the schema to a ``_test`` suffix.

    Order: ``EMS_TEST_DATABASE_URL``, then ``DATABASE_URL``, then ``DATABASE_URL`` as written
    in ``.env``. The schema rewrite is the safety interlock — pointing the suite at a
    development schema would let a test truncate real tables.
    """
    candidate = os.environ.get("EMS_TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not candidate:
        env_file = Path(__file__).resolve().parent.parent / ".env"
        if env_file.exists():
            for line in env_file.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if stripped.startswith("DATABASE_URL="):
                    candidate = stripped.split("=", 1)[1].strip().strip("\"'")
                    break
    if not candidate:
        return None

    match = _URL_WITH_SCHEMA.match(candidate)
    if match is None:
        return None
    schema = match.group("schema")
    if not schema.endswith(TEST_SCHEMA_SUFFIX):
        schema = f"{schema}{TEST_SCHEMA_SUFFIX}"
    return f"{match.group('prefix')}{schema}{match.group('query') or '?charset=utf8mb4'}"


def build_test_settings(**overrides: Any) -> Settings:
    """Construct :class:`Settings` for tests, bypassing the environment and ``.env``.

    ``_env_file=None`` is what stops pydantic-settings reading the developer's ``.env``, so a
    test run cannot pick up production-shaped configuration by accident.
    """
    database_url = overrides.pop(
        "database_url", _resolve_test_database_url() or "mysql+pymysql://unset@127.0.0.1/ems_test"
    )
    values: dict[str, Any] = {
        "environment": Environment.TEST,
        "debug": False,
        "database_url": database_url,
        "secret_key": secrets.token_urlsafe(48),
        "encryption_master_key": _generated_key(),
        "blind_index_master_key": _generated_key(),
        "key_provider": "env",
        "storage_backend": "local",
        "malware_scanner_backend": "noop",
        "log_level": "WARNING",
        "log_format": "text",
        "cors_origins": [],
        # Keep the pool tiny except where a concurrency test asks for more, so an exhausted
        # pool surfaces as a test failure rather than a hang.
        "db_pool_size": 5,
        "db_max_overflow": 10,
        "db_lock_wait_timeout_seconds": 5,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


@pytest.fixture(scope="session")
def test_settings() -> Settings:
    """Session-wide test settings."""
    return build_test_settings()


# --------------------------------------------------------------------------- database


@pytest.fixture(scope="session")
def database_url() -> str:
    """The test database URL, or skip the test when none is configured."""
    url = _resolve_test_database_url()
    if url is None:
        pytest.skip(SKIP_NO_DATABASE, allow_module_level=True)
    return url


@pytest.fixture(scope="session")
def database_available(database_url: str) -> bool:
    """Whether the test database accepts connections and is MySQL 8+."""
    engine = create_engine(database_url, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            version = str(connection.execute(text("SELECT VERSION()")).scalar())
    except Exception:
        pytest.skip(SKIP_NO_DATABASE, allow_module_level=True)
    finally:
        engine.dispose()

    major = version.split(".", 1)[0]
    if not major.isdigit() or int(major) < 8:
        pytest.skip(f"MySQL 8.0+ is required (ADR-002); server reports {version}")
    return True


@pytest.fixture(scope="session")
def engine(test_settings: Settings, database_available: bool) -> Iterator[Engine]:
    """A session-scoped engine configured exactly as the application configures its own."""
    from app.db.session import create_db_engine

    db_engine = create_db_engine(test_settings)
    yield db_engine
    db_engine.dispose()


@pytest.fixture(scope="session")
def migrated_database(engine: Engine, database_url: str) -> Iterator[Engine]:
    """Bring the test schema to ``head`` with Alembic, then hand back the engine.

    Migrations are applied through Alembic rather than ``metadata.create_all`` on purpose:
    the MySQL generated columns and check constraints that enforce the §23.3 catalogue live
    in the migration scripts, so a schema built by ``create_all`` would be missing exactly
    the constraints the integration tests exist to verify.
    """
    from alembic import command
    from alembic.config import Config

    project_root = Path(__file__).resolve().parent.parent
    alembic_config = Config(str(project_root / "alembic.ini"))
    alembic_config.set_main_option("script_location", str(project_root / "alembic"))
    alembic_config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(alembic_config, "head")
    yield engine


# --------------------------------------------------------------------------- api client


@pytest.fixture(scope="session")
def app(test_settings: Settings) -> Any:
    """The shared application, built once per session from test settings.

    Session-scoped deliberately. Building an application runs its lifespan, which opens a
    database connection, and the first connection to this MySQL server costs around two
    seconds; a function-scoped app made the contract suite twenty times slower for no gain.
    Tests that need to mount throwaway routes use :func:`isolated_app` instead, so the shared
    instance is never mutated.
    """
    from app.main import create_app

    return create_app(test_settings)


@pytest.fixture(scope="session")
def unauthenticated_client(app: Any) -> Iterator[Any]:
    """A test client with no credentials, for 401 and contract tests."""
    from fastapi.testclient import TestClient

    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.fixture(scope="class")
def isolated_app(test_settings: Settings) -> Any:
    """A fresh application for a single test.

    For tests that mount extra routes (to raise a specific error, for example). Keeping those
    off the shared instance stops one test class's throwaway route from being visible to
    another. Class-scoped rather than function-scoped for the same connection-cost reason
    as :func:`app`.
    """
    from app.main import create_app

    return create_app(test_settings)


# --------------------------------------------------------------------------- fixtures data

FICTITIOUS_PAN = "ABCPE1234F"
"""Structurally valid, deliberately fictitious (§1.4). Fourth character ``P`` marks an
individual, which §11.2 requires for an employee PAN."""

FICTITIOUS_IFSC = "SBIN0001234"
FICTITIOUS_ACCOUNT_NUMBER = "30123456789"


def fictitious_aadhaar() -> str:
    """A checksum-valid but fictitious Aadhaar, built from the algorithm.

    Generated rather than hardcoded so no literal that could resemble a real number appears
    in the repository (§9 of the brief).
    """
    from app.validation.checksums import verhoeff_check_digit

    body = "23456789012"
    return body + str(verhoeff_check_digit(body))


def fictitious_gstin(state_code: str = "24", pan: str = "AABCA1234C") -> str:
    """A checksum-valid but fictitious GSTIN whose PAN segment matches ``pan`` (§7.4)."""
    from app.validation.checksums import gstin_check_character

    body = f"{state_code}{pan}1Z"
    return body + gstin_check_character(body)


# --------------------------------------------------------------------------- markers


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Infer the suite marker from the test's directory.

    Keeps `-m integration` and `-m security` working without every file repeating a marker,
    and makes an unmarked test in a marked directory impossible.
    """
    directory_markers = {
        "unit": "unit",
        "integration": "integration",
        "security": "security",
        "concurrency": "concurrency",
        "acceptance": "acceptance",
    }
    for item in items:
        for part in item.path.parts:
            if (marker := directory_markers.get(part)) is not None:
                item.add_marker(getattr(pytest.mark, marker))
                break


# --------------------------------------------------------------------------- seeded database


@pytest.fixture(scope="session")
def seeded_database(migrated_database: Engine) -> Engine:
    """A migrated schema with the platform reference data in place.

    Truncates first, so a run starts from a known state rather than inheriting whatever the
    previous run left. Foreign-key checks are suspended for the truncation because the schema
    contains legitimate cycles (``users.employee_id`` and every ``created_by``), so no
    truncation order satisfies them all.
    """
    from sqlalchemy import inspect

    from app.services.seed_data import seed_platform

    with migrated_database.begin() as connection:
        tables = [
            name for name in inspect(connection).get_table_names() if name != "alembic_version"
        ]
        connection.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
        for table in tables:
            connection.execute(text(f"TRUNCATE TABLE `{table}`"))
        connection.execute(text("SET FOREIGN_KEY_CHECKS = 1"))

    from app.db.session import configure_engine, get_session_factory, unit_of_work

    configure_engine(migrated_database)
    session = get_session_factory()()
    try:
        with unit_of_work(session):
            seed_platform(session)
    finally:
        session.close()
    return migrated_database


@pytest.fixture
def db_session(seeded_database: Engine) -> Iterator[Any]:
    """A committing session for test setup.

    Setup data must be committed, because the application under test reads it through its own
    session on a different connection. Rolling back here would make the fixtures invisible to
    the endpoint.

    The handle is re-bound on every use: closing a ``TestClient`` runs the application's
    shutdown, which calls ``dispose_engine()``, and an unbound handle builds its engine from
    the ambient ``.env`` — so a test running after an API test would otherwise write into the
    developer's own schema instead of the ``_test`` one.
    """
    from app.db.session import configure_engine, get_session_factory

    configure_engine(seeded_database)

    session = get_session_factory()()
    try:
        yield session
        session.commit()
    finally:
        session.close()


@pytest.fixture
def api_app(test_settings: Settings, seeded_database: Engine) -> Any:
    """An application wired to the seeded test database."""
    from app.db.session import configure_engine
    from app.main import create_app

    configure_engine(seeded_database)
    return create_app(test_settings)


@pytest.fixture
def api_client(api_app: Any) -> Iterator[Any]:
    """An unauthenticated client against the seeded database."""
    from fastapi.testclient import TestClient

    with TestClient(api_app, raise_server_exceptions=False) as client:
        yield client


@pytest.fixture
def authenticate(api_client: Any) -> Any:
    """Return a helper that signs a user in and yields an authorised client wrapper.

    Logs in through the real ``/auth/login`` endpoint rather than minting a token directly, so
    every test exercises the same authentication path a client would.
    """
    from app.core.config import Settings as _Settings  # noqa: F401 - for type clarity

    def _login(user: Any) -> AuthorizedClient:
        response = api_client.post(
            "/api/v1/auth/login",
            json={"email": user.email, "password": user.password},
        )
        if response.status_code != 200:
            raise AssertionError(
                f"login failed for {user.email}: {response.status_code} {response.text}"
            )
        body = response.json()
        return AuthorizedClient(
            client=api_client,
            access_token=body["access_token"],
            refresh_token=body["refresh_token"],
        )

    return _login


class AuthorizedClient:
    """A thin wrapper adding the bearer header to each request."""

    __slots__ = ("access_token", "client", "refresh_token")

    def __init__(self, *, client: Any, access_token: str, refresh_token: str) -> None:
        self.client = client
        self.access_token = access_token
        self.refresh_token = refresh_token

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.access_token}"}

    def get(self, url: str, **kwargs: Any) -> Any:
        return self.client.get(url, headers={**self.headers, **kwargs.pop("headers", {})}, **kwargs)

    def post(self, url: str, **kwargs: Any) -> Any:
        return self.client.post(
            url, headers={**self.headers, **kwargs.pop("headers", {})}, **kwargs
        )

    def patch(self, url: str, **kwargs: Any) -> Any:
        return self.client.patch(
            url, headers={**self.headers, **kwargs.pop("headers", {})}, **kwargs
        )

    def put(self, url: str, **kwargs: Any) -> Any:
        return self.client.put(url, headers={**self.headers, **kwargs.pop("headers", {})}, **kwargs)

    def delete(self, url: str, **kwargs: Any) -> Any:
        return self.client.request(
            "DELETE", url, headers={**self.headers, **kwargs.pop("headers", {})}, **kwargs
        )
