"""attendance schema

Revision ID: d9a53b6ce232
Revises: c8f42a5bd121
Create Date: 2026-10-29 12:00:00.000000

Attendance addendum section 5, built in stages. Stage 1: the work-schedule configuration shared
with Leave (5.2) - weekly-off patterns, holiday calendars, holidays and branch calendar
assignments. Later stages append their tables to this same revision until the module ships.

Hand-review record: autogenerate output restricted to these five tables (it also emitted
unrelated use_alter foreign-key noise and drops of obsolete tables, all discarded). The
partial-unique rules of 5.2 are stored generated columns (``active_default_key``,
``active_date_key``), the MySQL emulation described in app/models/attendance.py.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

import app.db.types

revision: str = "d9a53b6ce232"
down_revision: Union[str, None] = "c8f42a5bd121"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('holiday_calendars',
    sa.Column('organization_id', app.db.types.Uuid7(length=16), nullable=False),
    sa.Column('calendar_code', sa.String(length=30), nullable=False),
    sa.Column('calendar_code_normalized', sa.String(length=30), sa.Computed('UPPER(calendar_code)', persisted=True), nullable=False),
    sa.Column('calendar_name', sa.String(length=150), nullable=False),
    sa.Column('is_organization_default', sa.Boolean(), server_default=sa.text('0'), nullable=False),
    sa.Column('status', sa.String(length=10), server_default='ACTIVE', nullable=False),
    sa.Column('active_default_key', sa.SmallInteger(), sa.Computed("CASE WHEN is_organization_default = 1 AND status = 'ACTIVE' THEN 1 END", persisted=True), nullable=True),
    sa.Column('updated_at', app.db.types.UtcDateTime(), server_default=sa.text('CURRENT_TIMESTAMP(6)'), nullable=False),
    sa.Column('updated_by', app.db.types.Uuid7(length=16), nullable=True),
    sa.Column('row_version', sa.Integer(), server_default=sa.text('1'), nullable=False),
    sa.Column('archived_at', app.db.types.UtcDateTime(), nullable=True),
    sa.Column('archived_by', app.db.types.Uuid7(length=16), nullable=True),
    sa.Column('archive_reason', sa.String(length=500), nullable=True),
    sa.Column('id', app.db.types.Uuid7(length=16), nullable=False),
    sa.Column('created_at', app.db.types.UtcDateTime(), server_default=sa.text('CURRENT_TIMESTAMP(6)'), nullable=False),
    sa.Column('created_by', app.db.types.Uuid7(length=16), nullable=True),
    sa.CheckConstraint("calendar_code REGEXP '^[A-Z0-9][A-Z0-9_./-]{0,29}$'", name='ck_holiday_calendars_code_pattern'),
    sa.CheckConstraint("status IN ('ACTIVE','INACTIVE')", name='ck_holiday_calendars_status'),
    sa.CheckConstraint('(archived_at IS NULL AND archive_reason IS NULL) OR (archived_at IS NOT NULL AND archive_reason IS NOT NULL AND CHAR_LENGTH(archive_reason) BETWEEN 10 AND 500)', name='ck_holiday_calendars_archive_reason'),
    sa.CheckConstraint('CHAR_LENGTH(calendar_name) BETWEEN 2 AND 150', name='ck_holiday_calendars_name_length'),
    sa.ForeignKeyConstraint(['archived_by'], ['users.id'], name=op.f('fk_holiday_calendars_archived_by'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], name=op.f('fk_holiday_calendars_created_by'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], name=op.f('fk_holiday_calendars_organization_id'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['updated_by'], ['users.id'], name=op.f('fk_holiday_calendars_updated_by'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_holiday_calendars')),
    sa.UniqueConstraint('organization_id', 'active_default_key', name='uq_holiday_calendars_active_default'),
    sa.UniqueConstraint('organization_id', 'calendar_code_normalized', name='uq_holiday_calendars_org_code'),
    sa.UniqueConstraint('organization_id', 'id', name='uq_holiday_calendars_org_id')
    )
    op.create_index('ix_holiday_calendars_org_status', 'holiday_calendars', ['organization_id', 'status'], unique=False)
    op.create_index(op.f('ix_holiday_calendars_organization_id'), 'holiday_calendars', ['organization_id'], unique=False)
    op.create_table('weekly_off_patterns',
    sa.Column('organization_id', app.db.types.Uuid7(length=16), nullable=False),
    sa.Column('pattern_code', sa.String(length=30), nullable=False),
    sa.Column('pattern_code_normalized', sa.String(length=30), sa.Computed('UPPER(pattern_code)', persisted=True), nullable=False),
    sa.Column('pattern_name', sa.String(length=100), nullable=False),
    sa.Column('status', sa.String(length=10), server_default='ACTIVE', nullable=False),
    sa.Column('updated_at', app.db.types.UtcDateTime(), server_default=sa.text('CURRENT_TIMESTAMP(6)'), nullable=False),
    sa.Column('updated_by', app.db.types.Uuid7(length=16), nullable=True),
    sa.Column('row_version', sa.Integer(), server_default=sa.text('1'), nullable=False),
    sa.Column('archived_at', app.db.types.UtcDateTime(), nullable=True),
    sa.Column('archived_by', app.db.types.Uuid7(length=16), nullable=True),
    sa.Column('archive_reason', sa.String(length=500), nullable=True),
    sa.Column('id', app.db.types.Uuid7(length=16), nullable=False),
    sa.Column('created_at', app.db.types.UtcDateTime(), server_default=sa.text('CURRENT_TIMESTAMP(6)'), nullable=False),
    sa.Column('created_by', app.db.types.Uuid7(length=16), nullable=True),
    sa.CheckConstraint("pattern_code REGEXP '^[A-Z0-9][A-Z0-9_./-]{0,29}$'", name='ck_weekly_off_patterns_code_pattern'),
    sa.CheckConstraint("status IN ('ACTIVE','INACTIVE')", name='ck_weekly_off_patterns_status'),
    sa.CheckConstraint('(archived_at IS NULL AND archive_reason IS NULL) OR (archived_at IS NOT NULL AND archive_reason IS NOT NULL AND CHAR_LENGTH(archive_reason) BETWEEN 10 AND 500)', name='ck_weekly_off_patterns_archive_reason'),
    sa.CheckConstraint('CHAR_LENGTH(pattern_name) BETWEEN 2 AND 100', name='ck_weekly_off_patterns_name_length'),
    sa.ForeignKeyConstraint(['archived_by'], ['users.id'], name=op.f('fk_weekly_off_patterns_archived_by'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], name=op.f('fk_weekly_off_patterns_created_by'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], name=op.f('fk_weekly_off_patterns_organization_id'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['updated_by'], ['users.id'], name=op.f('fk_weekly_off_patterns_updated_by'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_weekly_off_patterns')),
    sa.UniqueConstraint('organization_id', 'id', name='uq_weekly_off_patterns_org_id'),
    sa.UniqueConstraint('organization_id', 'pattern_code_normalized', name='uq_weekly_off_patterns_org_code')
    )
    op.create_index('ix_weekly_off_patterns_org_status', 'weekly_off_patterns', ['organization_id', 'status'], unique=False)
    op.create_index(op.f('ix_weekly_off_patterns_organization_id'), 'weekly_off_patterns', ['organization_id'], unique=False)
    op.create_table('branch_holiday_calendar_assignments',
    sa.Column('organization_id', app.db.types.Uuid7(length=16), nullable=False),
    sa.Column('branch_id', app.db.types.Uuid7(length=16), nullable=False),
    sa.Column('calendar_id', app.db.types.Uuid7(length=16), nullable=False),
    sa.Column('effective_from', sa.Date(), nullable=False),
    sa.Column('effective_to', sa.Date(), nullable=True),
    sa.Column('updated_at', app.db.types.UtcDateTime(), server_default=sa.text('CURRENT_TIMESTAMP(6)'), nullable=False),
    sa.Column('updated_by', app.db.types.Uuid7(length=16), nullable=True),
    sa.Column('row_version', sa.Integer(), server_default=sa.text('1'), nullable=False),
    sa.Column('archived_at', app.db.types.UtcDateTime(), nullable=True),
    sa.Column('archived_by', app.db.types.Uuid7(length=16), nullable=True),
    sa.Column('archive_reason', sa.String(length=500), nullable=True),
    sa.Column('id', app.db.types.Uuid7(length=16), nullable=False),
    sa.Column('created_at', app.db.types.UtcDateTime(), server_default=sa.text('CURRENT_TIMESTAMP(6)'), nullable=False),
    sa.Column('created_by', app.db.types.Uuid7(length=16), nullable=True),
    sa.CheckConstraint('(archived_at IS NULL AND archive_reason IS NULL) OR (archived_at IS NOT NULL AND archive_reason IS NOT NULL AND CHAR_LENGTH(archive_reason) BETWEEN 10 AND 500)', name='ck_branch_holiday_calendar_assignments_archive_reason'),
    sa.CheckConstraint('effective_to IS NULL OR effective_to > effective_from', name='ck_branch_calendar_assignments_range'),
    sa.ForeignKeyConstraint(['archived_by'], ['users.id'], name=op.f('fk_branch_holiday_calendar_assignments_archived_by'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], name=op.f('fk_branch_holiday_calendar_assignments_created_by'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['organization_id', 'branch_id'], ['branches.organization_id', 'branches.id'], name='fk_branch_calendar_assignments_branch', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['organization_id', 'calendar_id'], ['holiday_calendars.organization_id', 'holiday_calendars.id'], name='fk_branch_calendar_assignments_calendar', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], name=op.f('fk_branch_holiday_calendar_assignments_organization_id'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['updated_by'], ['users.id'], name=op.f('fk_branch_holiday_calendar_assignments_updated_by'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_branch_holiday_calendar_assignments')),
    sa.UniqueConstraint('branch_id', 'effective_from', name='uq_branch_calendar_assignments_branch_start')
    )
    op.create_unique_constraint('uq_branch_calendar_assignments_org_id', 'branch_holiday_calendar_assignments', ['organization_id', 'id'])
    op.create_index('ix_branch_calendar_assignments_org_branch', 'branch_holiday_calendar_assignments', ['organization_id', 'branch_id', 'effective_from'], unique=False)
    op.create_index(op.f('ix_branch_holiday_calendar_assignments_organization_id'), 'branch_holiday_calendar_assignments', ['organization_id'], unique=False)
    op.create_table('holidays',
    sa.Column('organization_id', app.db.types.Uuid7(length=16), nullable=False),
    sa.Column('calendar_id', app.db.types.Uuid7(length=16), nullable=False),
    sa.Column('holiday_date', sa.Date(), nullable=False),
    sa.Column('holiday_name', sa.String(length=150), nullable=False),
    sa.Column('holiday_type', sa.String(length=10), nullable=False),
    sa.Column('status', sa.String(length=10), server_default='ACTIVE', nullable=False),
    sa.Column('cancellation_reason', sa.String(length=500), nullable=True),
    sa.Column('active_date_key', sa.Date(), sa.Computed("CASE WHEN status = 'ACTIVE' THEN holiday_date END", persisted=True), nullable=True),
    sa.Column('updated_at', app.db.types.UtcDateTime(), server_default=sa.text('CURRENT_TIMESTAMP(6)'), nullable=False),
    sa.Column('updated_by', app.db.types.Uuid7(length=16), nullable=True),
    sa.Column('row_version', sa.Integer(), server_default=sa.text('1'), nullable=False),
    sa.Column('archived_at', app.db.types.UtcDateTime(), nullable=True),
    sa.Column('archived_by', app.db.types.Uuid7(length=16), nullable=True),
    sa.Column('archive_reason', sa.String(length=500), nullable=True),
    sa.Column('id', app.db.types.Uuid7(length=16), nullable=False),
    sa.Column('created_at', app.db.types.UtcDateTime(), server_default=sa.text('CURRENT_TIMESTAMP(6)'), nullable=False),
    sa.Column('created_by', app.db.types.Uuid7(length=16), nullable=True),
    sa.CheckConstraint("(status = 'CANCELLED') = (cancellation_reason IS NOT NULL) AND (cancellation_reason IS NULL OR CHAR_LENGTH(cancellation_reason) >= 10)", name='ck_holidays_cancellation_reason'),
    sa.CheckConstraint("holiday_type IN ('MANDATORY','OPTIONAL')", name='ck_holidays_type'),
    sa.CheckConstraint("status IN ('ACTIVE','CANCELLED')", name='ck_holidays_status'),
    sa.CheckConstraint('(archived_at IS NULL AND archive_reason IS NULL) OR (archived_at IS NOT NULL AND archive_reason IS NOT NULL AND CHAR_LENGTH(archive_reason) BETWEEN 10 AND 500)', name='ck_holidays_archive_reason'),
    sa.CheckConstraint('CHAR_LENGTH(holiday_name) BETWEEN 2 AND 150', name='ck_holidays_name'),
    sa.ForeignKeyConstraint(['archived_by'], ['users.id'], name=op.f('fk_holidays_archived_by'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], name=op.f('fk_holidays_created_by'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['organization_id', 'calendar_id'], ['holiday_calendars.organization_id', 'holiday_calendars.id'], name='fk_holidays_calendar', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], name=op.f('fk_holidays_organization_id'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['updated_by'], ['users.id'], name=op.f('fk_holidays_updated_by'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_holidays')),
    sa.UniqueConstraint('calendar_id', 'active_date_key', name='uq_holidays_calendar_active_date'),
    sa.UniqueConstraint('organization_id', 'id', name='uq_holidays_org_id')
    )
    op.create_index('ix_holidays_org_calendar_date', 'holidays', ['organization_id', 'calendar_id', 'holiday_date'], unique=False)
    op.create_index('ix_holidays_org_date', 'holidays', ['organization_id', 'holiday_date'], unique=False)
    op.create_index(op.f('ix_holidays_organization_id'), 'holidays', ['organization_id'], unique=False)
    op.create_table('weekly_off_pattern_days',
    sa.Column('organization_id', app.db.types.Uuid7(length=16), nullable=False),
    sa.Column('pattern_id', app.db.types.Uuid7(length=16), nullable=False),
    sa.Column('day_of_week', sa.SmallInteger(), nullable=False),
    sa.Column('occurrence', sa.String(length=10), nullable=False),
    sa.Column('id', app.db.types.Uuid7(length=16), nullable=False),
    sa.Column('created_at', app.db.types.UtcDateTime(), server_default=sa.text('CURRENT_TIMESTAMP(6)'), nullable=False),
    sa.Column('created_by', app.db.types.Uuid7(length=16), nullable=True),
    sa.CheckConstraint("occurrence IN ('ALL','FIRST','SECOND','THIRD','FOURTH','FIFTH','LAST')", name='ck_weekly_off_pattern_days_occurrence'),
    sa.CheckConstraint('day_of_week BETWEEN 1 AND 7', name='ck_weekly_off_pattern_days_day_of_week'),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], name=op.f('fk_weekly_off_pattern_days_created_by'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['organization_id', 'pattern_id'], ['weekly_off_patterns.organization_id', 'weekly_off_patterns.id'], name='fk_weekly_off_pattern_days_pattern', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], name=op.f('fk_weekly_off_pattern_days_organization_id'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_weekly_off_pattern_days')),
    sa.UniqueConstraint('pattern_id', 'day_of_week', 'occurrence', name='uq_weekly_off_pattern_days_rule')
    )
    op.create_unique_constraint('uq_weekly_off_pattern_days_org_id', 'weekly_off_pattern_days', ['organization_id', 'id'])
    op.create_index('ix_weekly_off_days_org_pattern', 'weekly_off_pattern_days', ['organization_id', 'pattern_id'], unique=False)
    op.create_index(op.f('ix_weekly_off_pattern_days_organization_id'), 'weekly_off_pattern_days', ['organization_id'], unique=False)


def downgrade() -> None:
    for table in (
        "weekly_off_pattern_days",
        "holidays",
        "branch_holiday_calendar_assignments",
        "weekly_off_patterns",
        "holiday_calendars",
    ):
        op.drop_table(table)  # dropping a table drops its indexes with it (DEC-049)
