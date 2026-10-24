"""Database engine, session factory and declarative base."""
from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import settings

engine_kwargs: dict = {"pool_pre_ping": True, "future": True}
if settings.is_sqlite:
    engine_kwargs["connect_args"] = {"check_same_thread": False}
elif settings.is_mysql:
    # MySQL closes idle connections after wait_timeout (default 8h).
    engine_kwargs.update(pool_recycle=280, pool_size=10, max_overflow=20)

engine = create_engine(settings.database_url, **engine_kwargs)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


class Base(DeclarativeBase):
    """Declarative base for every ORM model."""

    pass


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency that yields a scoped database session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def ensure_database() -> None:
    """Create the target MySQL schema if it does not exist yet.

    Connects to the server without selecting a database, so this works on a
    fresh MySQL instance. No-op for other backends.
    """
    if not settings.is_mysql:
        return
    url = make_url(settings.database_url)
    database = url.database
    if not database:
        return
    server_url = URL.create(
        drivername=url.drivername,
        username=url.username,
        password=url.password,
        host=url.host,
        port=url.port,
        database=None,
        query={"charset": "utf8mb4"},
    )
    database_url = settings.database_url

    if database_url.startswith("mysql://"):
        database_url = database_url.replace(
            "mysql://", "mysql+pymysql://", 1
        )
    elif database_url.startswith("mysql+mysqldb://"):
        database_url = database_url.replace(
            "mysql+mysqldb://", "mysql+pymysql://", 1
        )

    admin_engine = create_engine(database_url, **engine_kwargs)
    try:
        with admin_engine.connect() as conn:
            conn.exec_driver_sql(
                f"CREATE DATABASE IF NOT EXISTS `{database}` "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
    finally:
        admin_engine.dispose()


def init_db() -> None:
    """Create the database and all tables.

    Uses ``CREATE DATABASE IF NOT EXISTS`` on MySQL and
    ``Base.metadata.create_all`` everywhere. Production should use Alembic.
    """
    import app.models  # noqa: F401  (ensure models are registered)

    ensure_database()
    Base.metadata.create_all(bind=engine)


def check_connection() -> bool:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:  # pragma: no cover
        return False
