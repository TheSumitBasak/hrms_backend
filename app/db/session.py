"""Engine, session factory and the explicit unit of work.

Synchronous sessions (ADR-001). Transaction boundaries are explicit: repositories never
commit, services wrap their work in :func:`unit_of_work`, and the request-scoped dependency
in :mod:`app.api.deps` rolls back anything left open.

Every connection is configured on checkout so the guarantees the spec relies on hold for all
sessions rather than depending on server defaults:

``transaction_isolation = READ-COMMITTED``
    §18.8 specifies READ COMMITTED plus an explicit employee-row lock as sufficient for the
    salary timeline. MySQL defaults to REPEATABLE READ, under which the in-transaction
    overlap check would read a stale snapshot and the compensating control for INV-1 would
    not actually work.

``sql_mode`` includes ``STRICT_ALL_TABLES``
    Without it MySQL coerces out-of-range values and over-long strings instead of raising, so
    a ``VARCHAR(10)`` identifier would be silently truncated — a data-integrity failure that
    no amount of application validation can catch after the fact.

``time_zone = '+00:00'``
    So ``CURRENT_TIMESTAMP(6)`` server defaults are UTC, matching :class:`UtcDateTime`.

``innodb_lock_wait_timeout``
    Bounded so a contended employee or sequence row surfaces as a retryable
    ``409 CONCURRENT_MODIFICATION`` rather than hanging the request (§24.3).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.orm.exc import StaleDataError
from sqlalchemy.pool import QueuePool

from app.core.config import Settings, get_settings
from app.core.errors import (
    ConcurrentModification,
    DuplicateResource,
    EmsError,
    InvalidOrganizationReference,
    PreconditionFailed,
    ValidationFailed,
)

logger = logging.getLogger(__name__)

REQUIRED_SQL_MODE = (
    "STRICT_ALL_TABLES,"
    "ERROR_FOR_DIVISION_BY_ZERO,"
    "NO_ENGINE_SUBSTITUTION,"
    "NO_ZERO_DATE,"
    "NO_ZERO_IN_DATE"
)
"""Modes this application requires. **Added** to the server's own modes, never substituted
for them.

Replacing ``sql_mode`` outright looks tidier but is a correctness bug. SQLAlchemy's MySQL
dialect reads ``sql_mode`` once, at dialect initialisation, to decide how to quote
identifiers — a server running in ``ANSI``/``ANSI_QUOTES`` mode makes ``"`` an identifier
quote. If the connect hook then stripped ``ANSI_QUOTES``, the dialect would keep emitting
``"name"`` while the session had stopped accepting it, and every reflection query would fail
with a syntax error. Appending keeps the session consistent with what the dialect detected,
whatever the server administrator has configured.
"""

# MySQL error numbers we translate into domain errors rather than leaking.
MYSQL_DUPLICATE_ENTRY = 1062
MYSQL_FK_PARENT_MISSING = 1452
MYSQL_FK_CHILD_EXISTS = 1451
MYSQL_LOCK_WAIT_TIMEOUT = 1205
MYSQL_DEADLOCK = 1213
MYSQL_CHECK_CONSTRAINT_VIOLATED = 3819
MYSQL_DATA_TOO_LONG = 1406
MYSQL_OUT_OF_RANGE = 1264

_RETRYABLE_MYSQL_ERRORS = frozenset({MYSQL_LOCK_WAIT_TIMEOUT, MYSQL_DEADLOCK})


def create_db_engine(settings: Settings | None = None) -> Engine:
    """Build the engine. One per process; the API composes it in the app factory."""
    config = settings or get_settings()
    engine = create_engine(
        config.database_url,
        poolclass=QueuePool,
        pool_size=config.db_pool_size,
        max_overflow=config.db_max_overflow,
        pool_recycle=config.db_pool_recycle_seconds,
        pool_pre_ping=config.db_pool_pre_ping,
        echo=config.db_echo,
        future=True,
        # Values are already Decimal/UUID-typed by our column types; this keeps pymysql from
        # re-interpreting DECIMAL as float and losing precision on money columns.
        connect_args={"charset": "utf8mb4"},
    )
    _register_session_setup(engine, config)
    return engine


def _register_session_setup(engine: Engine, settings: Settings) -> None:
    """Apply the session variables above to every pooled connection on checkout."""

    @event.listens_for(engine, "connect")
    def _configure_connection(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("SET SESSION transaction_isolation = 'READ-COMMITTED'")
            # Union of the server's modes and ours, so nothing the dialect relies on is lost.
            cursor.execute(
                "SET SESSION sql_mode = CONCAT(@@SESSION.sql_mode, %s)",
                (f",{REQUIRED_SQL_MODE}",),
            )
            cursor.execute("SET SESSION time_zone = '+00:00'")
            cursor.execute(
                "SET SESSION innodb_lock_wait_timeout = %s",
                (settings.db_lock_wait_timeout_seconds,),
            )
        finally:
            cursor.close()


def _build_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Session factory for ``engine``.

    ``expire_on_commit=False`` so a service can still read the entity it just wrote in order
    to build a response, without issuing a second SELECT after commit.
    """
    return sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
        class_=Session,
    )


class _DatabaseHandle:
    """Holds the process engine and session factory.

    A single module-level instance rather than module globals mutated through ``global``
    statements: the lifecycle (lazy create, replace for tests and jobs, dispose on shutdown)
    is explicit and the state is encapsulated. Creation is lock-guarded because endpoints are
    served from a threadpool (ADR-001), so two threads can race the first request.
    """

    __slots__ = ("_engine", "_factory", "_lock")

    def __init__(self) -> None:
        self._engine: Engine | None = None
        self._factory: sessionmaker[Session] | None = None
        self._lock = threading.Lock()

    def engine(self) -> Engine:
        with self._lock:
            if self._engine is None:
                self._engine = create_db_engine()
                self._factory = _build_session_factory(self._engine)
            return self._engine

    def session_factory(self) -> sessionmaker[Session]:
        with self._lock:
            if self._factory is None:
                self._engine = self._engine or create_db_engine()
                self._factory = _build_session_factory(self._engine)
            return self._factory

    def configure(self, engine: Engine) -> None:
        """Replace the engine. Used by the test harness and by job entrypoints."""
        with self._lock:
            self._engine = engine
            self._factory = _build_session_factory(engine)

    def dispose(self) -> None:
        """Close pooled connections. Called on application shutdown."""
        with self._lock:
            if self._engine is not None:
                self._engine.dispose()
            self._engine = None
            self._factory = None


_database = _DatabaseHandle()


def get_engine() -> Engine:
    """The process engine, created on first use."""
    return _database.engine()


def get_session_factory() -> sessionmaker[Session]:
    """The process session factory, created on first use."""
    return _database.session_factory()


def configure_engine(engine: Engine) -> None:
    """Point the process at ``engine``."""
    _database.configure(engine)


def dispose_engine() -> None:
    """Release the process engine and its pool."""
    _database.dispose()


@contextmanager
def unit_of_work(session: Session) -> Iterator[Session]:
    """Explicit transaction boundary.

    Commits on success, rolls back on any exception, and translates database-level failures
    into domain errors on the way out. A service's entire use case — business rows, history
    rows and the audit record — runs inside one of these, which is how NFR-10's "audit
    written in the same transaction as the change" is guaranteed structurally rather than by
    remembering to do it.
    """
    try:
        yield session
        session.commit()
    except EmsError:
        session.rollback()
        raise
    except StaleDataError as exc:
        # SQLAlchemy's version_id_col found row_version had moved on: a lost update.
        session.rollback()
        raise PreconditionFailed() from exc
    except DBAPIError as exc:
        session.rollback()
        raise translate_database_error(exc) from exc
    except Exception:
        session.rollback()
        raise


# MySQL error number -> the domain error it means. Keyed on the **error number**, not on the
# SQLAlchemy wrapper class, because the two do not line up: MySQL reports a violated CHECK
# constraint (3819) and an over-long value (1406) as `OperationalError`, while a duplicate key
# (1062) and a failed foreign key (1452) arrive as `IntegrityError`. Dispatching on the
# exception class let CHECK violations fall through to the generic handler and surface as a 500
# instead of the 422 the contract promises.
_ERROR_TRANSLATIONS: dict[int, Callable[[], EmsError]] = {
    MYSQL_DUPLICATE_ENTRY: DuplicateResource,
    # A composite organization FK rejected the row: the referenced master or employee lives in
    # another organization (§6.3, AC-03).
    MYSQL_FK_PARENT_MISSING: InvalidOrganizationReference,
    MYSQL_FK_CHILD_EXISTS: lambda: ValidationFailed(
        "The record is referenced by other records and cannot be removed."
    ),
    MYSQL_CHECK_CONSTRAINT_VIOLATED: ValidationFailed,
    MYSQL_DATA_TOO_LONG: lambda: ValidationFailed("A supplied value is too long for its field."),
    MYSQL_OUT_OF_RANGE: lambda: ValidationFailed("A supplied value is out of range."),
    MYSQL_LOCK_WAIT_TIMEOUT: ConcurrentModification,
    MYSQL_DEADLOCK: ConcurrentModification,
}


def translate_database_error(exc: DBAPIError) -> EmsError:
    """Map a database failure onto a domain error.

    Database constraints are a real enforcement layer here, not a backstop, so a violation must
    produce the same error contract as the equivalent service check. The constraint *name* is
    deliberately not echoed to the client — it would disclose schema internals — and is logged
    instead.

    An unrecognised error is re-raised rather than flattened into a 422: guessing at an unknown
    database failure would hide a real fault behind a validation message.
    """
    code = _mysql_error_code(exc)
    translation = _ERROR_TRANSLATIONS.get(code) if code is not None else None
    if translation is None:
        raise exc

    if code in _RETRYABLE_MYSQL_ERRORS:
        logger.info("lock contention", extra={"mysql_error": code})
    else:
        logger.warning(
            "database constraint violation",
            extra={"mysql_error": code, "constraint": _constraint_name(exc)},
        )
    return translation()


def _mysql_error_code(exc: DBAPIError) -> int | None:
    """Extract the numeric MySQL error code from a DBAPI exception."""
    args = getattr(exc.orig, "args", None)
    if args and isinstance(args[0], int):
        return args[0]
    return None


def _constraint_name(exc: DBAPIError) -> str | None:
    """Best-effort constraint name, for the log record only."""
    args = getattr(exc.orig, "args", None)
    if not args or len(args) < 2 or not isinstance(args[1], str):
        return None
    message = args[1]
    for marker in ("key '", "CONSTRAINT `", "constraint '"):
        if marker in message:
            tail = message.split(marker, 1)[1]
            return tail.split("'")[0].split("`")[0]
    return None


def check_connection(engine: Engine | None = None) -> bool:
    """Readiness probe: can we reach the database and run a trivial statement?"""
    try:
        with (engine or get_engine()).connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception:  # a probe must report false, never propagate
        logger.warning("database readiness check failed", exc_info=True)
        return False
    return True


__all__ = [
    "REQUIRED_SQL_MODE",
    "check_connection",
    "configure_engine",
    "create_db_engine",
    "dispose_engine",
    "get_engine",
    "get_session_factory",
    "translate_database_error",
    "unit_of_work",
]
