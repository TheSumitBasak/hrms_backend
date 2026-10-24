"""user prefs, report schedules, tenant settings, avatar, loan flag

Revision ID: 01f4c4eff7a6
Revises: 3a484b772fc5
Create Date: 2026-09-23 22:04:10.361163
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '01f4c4eff7a6'
down_revision: Union[str, None] = '3a484b772fc5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'user_preferences',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column('active_org_id', sa.Uuid(), nullable=True),
        sa.Column('dashboard_layout', sa.JSON(), nullable=True),
        sa.Column('ui_prefs', sa.JSON(), nullable=True),
        sa.Column('locale', sa.String(length=10), nullable=True),
        sa.Column('theme', sa.String(length=20), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.ForeignKeyConstraint(['active_org_id'], ['organizations.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id'),
    )
    op.create_table(
        'report_schedules',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('org_id', sa.Uuid(), nullable=False),
        sa.Column('report_code', sa.String(length=100), nullable=False),
        sa.Column('frequency', sa.String(length=20), nullable=False),
        sa.Column('format', sa.String(length=10), nullable=False),
        sa.Column('recipients', sa.JSON(), nullable=True),
        sa.Column('next_run_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.text('1')),
        sa.Column('created_by', sa.Uuid(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['org_id'], ['organizations.id']),
        sa.ForeignKeyConstraint(['created_by'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_report_schedules_org_id', 'report_schedules', ['org_id'])
    op.create_table(
        'tenant_settings',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('org_id', sa.Uuid(), nullable=False),
        sa.Column('key', sa.String(length=100), nullable=False),
        sa.Column('value', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['org_id'], ['organizations.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('org_id', 'key', name='uq_tenant_setting_key'),
    )
    op.create_index('ix_tenant_settings_org_id', 'tenant_settings', ['org_id'])
    op.add_column('employees', sa.Column('avatar_path', sa.String(length=255), nullable=True))
    op.add_column(
        'loans',
        sa.Column('paid_outside_payroll', sa.Boolean(), nullable=False, server_default=sa.text('0')),
    )


def downgrade() -> None:
    op.drop_column('loans', 'paid_outside_payroll')
    op.drop_column('employees', 'avatar_path')
    op.drop_index('ix_tenant_settings_org_id', table_name='tenant_settings')
    op.drop_table('tenant_settings')
    op.drop_index('ix_report_schedules_org_id', table_name='report_schedules')
    op.drop_table('report_schedules')
    op.drop_table('user_preferences')
