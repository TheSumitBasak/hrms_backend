"""Drop every table in a **development** EMS schema, for migration iteration.

    python scripts/reset_schema.py ems_test

Refuses any schema whose name is not in :data:`ALLOWED_SCHEMAS`. MySQL DDL is not
transactional, so a failed migration can leave a schema half-built with no way forward
except starting clean; this exists for exactly that case and for nothing else.

**Never run this against a schema holding real data.** It is not invoked by the application,
the test suite or any migration.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pymysql

# Runnable as a plain script from the repository root, which is how an operator will
# invoke it, so the root goes on the path before the sibling import.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.create_databases import _URL_PATTERN, read_database_url

ALLOWED_SCHEMAS = frozenset({"ems", "ems_test"})
"""Development schemas only. The guard is a hard allowlist, not a naming heuristic."""


def main(argv: list[str] | None = None) -> int:
    arguments = argv if argv is not None else sys.argv[1:]
    schema = arguments[0] if arguments else "ems_test"
    if schema not in ALLOWED_SCHEMAS:
        raise SystemExit(
            f"refusing to reset {schema!r}: only {sorted(ALLOWED_SCHEMAS)} may be reset"
        )
    if not re.fullmatch(r"[A-Za-z0-9_]+", schema):
        raise SystemExit(f"unsafe schema name: {schema!r}")

    match = _URL_PATTERN.match(read_database_url())
    if match is None:
        raise SystemExit("could not parse DATABASE_URL")

    connection = pymysql.connect(
        host=match.group("host"),
        port=int(match.group("port") or 3306),
        user=match.group("user"),
        password=match.group("password") or "",
        database=schema,
        charset="utf8mb4",
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = %s",
                (schema,),
            )
            tables = [row[0] for row in cursor.fetchall()]
            if not tables:
                print(f"{schema}: already empty")
                return 0
            # Foreign keys form cycles here, so constraint checking is suspended for the
            # drop rather than attempting a topological order.
            cursor.execute("SET FOREIGN_KEY_CHECKS = 0")
            for table in tables:
                cursor.execute(f"DROP TABLE IF EXISTS `{table}`")
            cursor.execute("SET FOREIGN_KEY_CHECKS = 1")
        connection.commit()
        print(f"{schema}: dropped {len(tables)} tables")
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
