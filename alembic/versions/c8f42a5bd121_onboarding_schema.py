"""onboarding schema

Revision ID: c8f42a5bd121
Revises: b7e31f4a9c10
Create Date: 2026-10-29 12:00:00.000000

Reserved slot in the migration chain; the owning module fills in ``upgrade``/``downgrade``.
"""

from typing import Sequence, Union

revision: str = "c8f42a5bd121"
down_revision: Union[str, None] = "b7e31f4a9c10"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
