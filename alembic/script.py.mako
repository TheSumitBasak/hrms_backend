"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}

Review checklist before committing this revision (docs/DATABASE.md):
  * MySQL DDL is not transactional — a failure leaves the schema partly changed.
  * Autogenerate does not reliably emit data migrations or constraint renames.
  * Confirm that stored generated columns and FULLTEXT indexes survived generation.
  * Confirm the downgrade is correct, or state explicitly why it is not reversible.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# The EMS schema uses custom column types (UUIDv7 as BINARY(16), UTC DATETIME(6)), which
# autogenerate renders as `app.db.types.X()`. Imported here so every revision is runnable.
import app.db.types
${imports if imports else ""}

revision: str = ${repr(up_revision)}
down_revision: Union[str, None] = ${repr(down_revision)}
branch_labels: Union[str, Sequence[str], None] = ${repr(branch_labels)}
depends_on: Union[str, Sequence[str], None] = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
