"""seed position and headcount permissions and role links

Revision ID: c4a71e5d9f08
Revises: ab9c69256eac
Create Date: 2026-10-29 04:20:00.000000

The five JA-SPEC-001 §12.1 permissions and the §12.2 ``Y`` cells of the role matrix. Only the
``Y`` cells are linked: ``position.manage``, ``headcount.manage`` and ``headcount.approve``
are ``C`` cells for HR Admin, which are per-organization opt-ins in
``organization_role_permissions`` (DEC-004). Seeding them platform-wide would hand every
organization a permission the specification says is off by default.

Idempotent, like the leave permission revision: each insert is guarded by NOT EXISTS, so it
can run on a database where `app.cli seed` has already created the rows from the code matrix.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "c4a71e5d9f08"
down_revision: Union[str, None] = "ab9c69256eac"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

PERMISSIONS: tuple[tuple[str, str], ...] = (
    ("position.view", "View positions, occupancy and position history"),
    ("position.manage", "Create and edit positions and run their lifecycle"),
    ("headcount.view", "View headcount summaries, plans and change requests"),
    ("headcount.manage", "Create and submit headcount plans and change requests"),
    ("headcount.approve", "Approve or reject headcount plans and change requests"),
)

# §12.2 "Y" cells only.
ROLE_GRANTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "OA",
        (
            "position.view",
            "position.manage",
            "headcount.view",
            "headcount.manage",
            "headcount.approve",
        ),
    ),
    ("HRA", ("position.view", "headcount.view")),
    ("HRE", ("position.view",)),
)

ALL_CODES = tuple(code for code, _ in PERMISSIONS)


def _code_list(codes: tuple[str, ...]) -> str:
    return ",".join(f"'{code}'" for code in codes)


def upgrade() -> None:
    for code, description in PERMISSIONS:
        op.execute(
            "INSERT INTO permissions (id, permission_code, description) "
            "SELECT UNHEX(REPLACE(UUID(),'-','')), "
            f"'{code}', '{description}' FROM DUAL "
            f"WHERE NOT EXISTS (SELECT 1 FROM permissions WHERE permission_code='{code}')"
        )
    for role_code, codes in ROLE_GRANTS:
        op.execute(
            "INSERT INTO role_permissions (role_id, permission_id) "
            "SELECT r.id, p.id FROM roles r "
            f"JOIN permissions p ON p.permission_code IN ({_code_list(codes)}) "
            f"WHERE r.role_code='{role_code}' AND NOT EXISTS ("
            "SELECT 1 FROM role_permissions rp "
            "WHERE rp.role_id=r.id AND rp.permission_id=p.id)"
        )


def downgrade() -> None:
    codes = _code_list(ALL_CODES)
    op.execute(
        "DELETE rp FROM role_permissions rp JOIN permissions p ON p.id=rp.permission_id "
        f"WHERE p.permission_code IN ({codes})"
    )
    op.execute(
        "DELETE orp FROM organization_role_permissions orp "
        f"WHERE orp.permission_code IN ({codes})"
    )
    op.execute(f"DELETE FROM permissions WHERE permission_code IN ({codes})")
