"""salary component restructure: display name, type, system Basic, computed percentage amount

Revision ID: e1a2b3c4d5f6
Revises: c6f93e2a8b74
Create Date: 2026-11-02 10:00:00.000000

* ``salary_components``: ``display_name`` (backfilled from ``component_name``),
  ``component_type`` (DEDUCTION for the old EMPLOYEE_DEDUCTION_REFERENCE category, else
  EARNING) and ``is_system``.
* ``employee_salary_components``: matching snapshots plus ``computed_amount``. Existing
  PERCENTAGE rows keep a null ``computed_amount``: they predate "% of Basic" and were labels.
* Every organization gets its system BASIC component. An existing component already coded
  BASIC is promoted to system rather than duplicated.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e1a2b3c4d5f6"
down_revision: str | None = "c6f93e2a8b74"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("salary_components", sa.Column("display_name", sa.String(100), nullable=True))
    op.add_column(
        "salary_components",
        sa.Column("component_type", sa.String(10), nullable=False, server_default="EARNING"),
    )
    op.add_column(
        "salary_components",
        sa.Column("is_system", sa.Boolean(), nullable=False, server_default=sa.text("0")),
    )
    op.execute("UPDATE salary_components SET display_name = component_name")
    op.execute(
        "UPDATE salary_components SET component_type = 'DEDUCTION' "
        "WHERE component_category = 'EMPLOYEE_DEDUCTION_REFERENCE'"
    )
    op.alter_column(
        "salary_components", "display_name", existing_type=sa.String(100), nullable=False
    )
    op.create_check_constraint(
        "ck_salary_components_type",
        "salary_components",
        "component_type IN ('EARNING','DEDUCTION')",
    )

    op.add_column(
        "employee_salary_components",
        sa.Column("display_name_snapshot", sa.String(100), nullable=True),
    )
    op.add_column(
        "employee_salary_components",
        sa.Column(
            "component_type_snapshot", sa.String(10), nullable=False, server_default="EARNING"
        ),
    )
    op.add_column(
        "employee_salary_components",
        sa.Column("computed_amount", sa.Numeric(15, 2), nullable=True),
    )
    op.execute(
        "UPDATE employee_salary_components SET display_name_snapshot = component_name_snapshot"
    )
    op.execute(
        "UPDATE employee_salary_components SET component_type_snapshot = 'DEDUCTION' "
        "WHERE component_category_snapshot = 'EMPLOYEE_DEDUCTION_REFERENCE'"
    )
    op.alter_column(
        "employee_salary_components",
        "display_name_snapshot",
        existing_type=sa.String(100),
        nullable=False,
    )
    op.create_check_constraint(
        "ck_employee_salary_components_type",
        "employee_salary_components",
        "component_type_snapshot IN ('EARNING','DEDUCTION')",
    )

    op.execute(
        "UPDATE salary_components SET is_system = 1 WHERE component_code_normalized = 'BASIC'"
    )
    op.execute(
        """
        INSERT INTO salary_components
            (id, organization_id, component_code, component_name, display_name, component_type,
             component_category, value_type, amount_period, currency, is_required,
             is_editable_per_employee, is_system, display_order, status)
        SELECT UNHEX(REPLACE(UUID(), '-', '')), o.id, 'BASIC', 'Basic', 'Basic', 'EARNING',
               'EARNINGS', 'FIXED_AMOUNT', 'MONTHLY', o.default_currency, 1, 1, 1, 0, 'ACTIVE'
        FROM organizations o
        WHERE NOT EXISTS (
            SELECT 1 FROM salary_components c
            WHERE c.organization_id = o.id AND c.component_code_normalized = 'BASIC'
        )
        """
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_employee_salary_components_type", "employee_salary_components", type_="check"
    )
    op.drop_column("employee_salary_components", "computed_amount")
    op.drop_column("employee_salary_components", "component_type_snapshot")
    op.drop_column("employee_salary_components", "display_name_snapshot")
    op.drop_constraint("ck_salary_components_type", "salary_components", type_="check")
    op.drop_column("salary_components", "is_system")
    op.drop_column("salary_components", "component_type")
    op.drop_column("salary_components", "display_name")
