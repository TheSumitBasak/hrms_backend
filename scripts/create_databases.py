"""Create the EMS schemas on a local MySQL server.

    python scripts/create_databases.py            # creates `ems` and `ems_test`
    python scripts/create_databases.py --name foo # creates `foo` and `foo_test`

Connection parameters come from ``DATABASE_URL`` (environment or ``.env``), but the schema
name comes from ``--name`` and defaults to :data:`DEFAULT_SCHEMA`. The two are deliberately
decoupled: this repository previously hosted an unrelated HRMS whose schema defines
``employees``, ``organizations``, ``audit_logs`` and ``salary_components`` with different
columns, so EMS must own its own schema rather than migrate into a pre-existing one.

``CREATE DATABASE IF NOT EXISTS`` only: this script never drops or alters an existing schema,
and it never prints the connection URL, which contains credentials. Tables are created by
``alembic upgrade head``, never by this script (§10 of the brief).
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

import pymysql

DEFAULT_SCHEMA = "ems"

CHARSET = "utf8mb4"
COLLATION = "utf8mb4_0900_ai_ci"
"""MySQL 8 default Unicode collation; accent- and case-insensitive, which matches the
case-insensitive comparison §23.1 specifies for codes and emails."""

_URL_PATTERN = re.compile(
    r"^mysql\+pymysql://(?P<user>[^:@/]+)(?::(?P<password>[^@/]*))?@"
    r"(?P<host>[^:/]+)(?::(?P<port>\d+))?/(?P<database>[^?]+)"
)


def read_database_url() -> str:
    """Resolve ``DATABASE_URL`` from the process environment, falling back to ``.env``."""
    if from_env := os.environ.get("DATABASE_URL"):
        return from_env
    env_file = Path(__file__).resolve().parent.parent / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("DATABASE_URL="):
                return stripped.split("=", 1)[1].strip().strip("\"'")
    raise SystemExit(
        "DATABASE_URL is not set. Copy .env.example to .env and fill it in, or export "
        "DATABASE_URL before running this script."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create the EMS schemas.")
    parser.add_argument(
        "--name",
        default=os.environ.get("EMS_SCHEMA_NAME", DEFAULT_SCHEMA),
        help=f"base schema name (default: {DEFAULT_SCHEMA}); '<name>_test' is also created",
    )
    arguments = parser.parse_args(argv)

    url = read_database_url()
    match = _URL_PATTERN.match(url)
    if match is None:
        raise SystemExit("DATABASE_URL must look like mysql+pymysql://user:pass@host:3306/name")

    schemas = [arguments.name, f"{arguments.name}_test"]

    connection = pymysql.connect(
        host=match.group("host"),
        port=int(match.group("port") or 3306),
        user=match.group("user"),
        password=match.group("password") or "",
        charset=CHARSET,
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT VERSION()")
            row = cursor.fetchone()
            version = row[0] if row else "unknown"
            print(f"connected to MySQL {version}")
            for schema in schemas:
                # Identifiers cannot be bound as parameters, so the name is validated instead.
                if not re.fullmatch(r"[A-Za-z0-9_]+", schema):
                    raise SystemExit(f"refusing to use unsafe schema name: {schema!r}")
                cursor.execute(
                    f"CREATE DATABASE IF NOT EXISTS `{schema}` "
                    f"CHARACTER SET {CHARSET} COLLATE {COLLATION}"
                )
                print(f"  schema ready: {schema}")
        connection.commit()
    finally:
        connection.close()

    print("\nNext: alembic upgrade head")
    return 0


if __name__ == "__main__":
    sys.exit(main())
