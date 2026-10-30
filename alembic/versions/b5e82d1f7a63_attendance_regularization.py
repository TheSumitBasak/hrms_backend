"""attendance regularization schema

Revision ID: b5e82d1f7a63
Revises: a3d91c7e5b42
Create Date: 2026-10-29 18:00:00.000000

Hand-review record: autogenerate output restricted to the tables below (unrelated use_alter
foreign-key noise discarded). The one-pending-request rule is a stored generated column (pending_key).
A new revision rather than an append to d9a53b6ce232, which is already applied (DEC-OAL-3).
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

import app.db.types

revision: str = "b5e82d1f7a63"
down_revision: Union[str, None] = "a3d91c7e5b42"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('attendance_regularization_requests',
    sa.Column('organization_id', app.db.types.Uuid7(length=16), nullable=False),
    sa.Column('employee_id', app.db.types.Uuid7(length=16), nullable=False),
    sa.Column('attendance_record_id', app.db.types.Uuid7(length=16), nullable=False),
    sa.Column('business_date', sa.Date(), nullable=False),
    sa.Column('request_type', sa.String(length=20), nullable=False),
    sa.Column('reason', sa.String(length=1000), nullable=False),
    sa.Column('supporting_document_id', app.db.types.Uuid7(length=16), nullable=True),
    sa.Column('status', sa.String(length=10), nullable=False),
    sa.Column('pending_key', sa.SmallInteger(), sa.Computed("CASE WHEN status = 'SUBMITTED' THEN 1 END", persisted=True), nullable=True),
    sa.Column('approval_chain_snapshot', sa.String(length=20), nullable=False),
    sa.Column('current_approval_level', sa.SmallInteger(), nullable=True),
    sa.Column('current_approver_type', sa.String(length=10), nullable=True),
    sa.Column('current_approver_user_id', app.db.types.Uuid7(length=16), nullable=True),
    sa.Column('current_approver_employee_id', app.db.types.Uuid7(length=16), nullable=True),
    sa.Column('submitted_by', app.db.types.Uuid7(length=16), nullable=False),
    sa.Column('submitted_at', app.db.types.UtcDateTime(), nullable=False),
    sa.Column('submitted_on_behalf', sa.Boolean(), nullable=False),
    sa.Column('decided_by', app.db.types.Uuid7(length=16), nullable=True),
    sa.Column('decided_at', app.db.types.UtcDateTime(), nullable=True),
    sa.Column('decision_comment', sa.String(length=1000), nullable=True),
    sa.Column('resubmission_of_request_id', app.db.types.Uuid7(length=16), nullable=True),
    sa.Column('cancellation_reason', sa.String(length=500), nullable=True),
    sa.Column('updated_at', app.db.types.UtcDateTime(), server_default=sa.text('CURRENT_TIMESTAMP(6)'), nullable=False),
    sa.Column('updated_by', app.db.types.Uuid7(length=16), nullable=True),
    sa.Column('row_version', sa.Integer(), server_default=sa.text('1'), nullable=False),
    sa.Column('archived_at', app.db.types.UtcDateTime(), nullable=True),
    sa.Column('archived_by', app.db.types.Uuid7(length=16), nullable=True),
    sa.Column('archive_reason', sa.String(length=500), nullable=True),
    sa.Column('id', app.db.types.Uuid7(length=16), nullable=False),
    sa.Column('created_at', app.db.types.UtcDateTime(), server_default=sa.text('CURRENT_TIMESTAMP(6)'), nullable=False),
    sa.Column('created_by', app.db.types.Uuid7(length=16), nullable=True),
    sa.CheckConstraint("current_approver_type IS NULL OR current_approver_type IN ('MANAGER','HR_QUEUE')", name='ck_att_reg_requests_approver'),
    sa.CheckConstraint("request_type IN ('MISSING_PUNCH','PUNCH_CORRECTION','ON_DUTY')", name='ck_att_reg_requests_type'),
    sa.CheckConstraint("status IN ('SUBMITTED','APPROVED','REJECTED','WITHDRAWN','CANCELLED')", name='ck_att_reg_requests_status'),
    sa.CheckConstraint('CHAR_LENGTH(reason) BETWEEN 10 AND 1000', name='ck_att_reg_requests_reason'),
    sa.ForeignKeyConstraint(['archived_by'], ['users.id'], name=op.f('fk_attendance_regularization_requests_archived_by'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], name=op.f('fk_attendance_regularization_requests_created_by'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['current_approver_user_id'], ['users.id'], name=op.f('fk_attendance_regularization_requests_current_approver_user_id'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['decided_by'], ['users.id'], name=op.f('fk_attendance_regularization_requests_decided_by'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['organization_id', 'attendance_record_id'], ['attendance_records.organization_id', 'attendance_records.id'], name='fk_att_reg_requests_record', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['organization_id', 'employee_id'], ['employees.organization_id', 'employees.id'], name='fk_att_reg_requests_employee', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['organization_id', 'supporting_document_id'], ['employee_documents.organization_id', 'employee_documents.id'], name='fk_att_reg_requests_document', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], name=op.f('fk_attendance_regularization_requests_organization_id'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['submitted_by'], ['users.id'], name=op.f('fk_attendance_regularization_requests_submitted_by'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['updated_by'], ['users.id'], name=op.f('fk_attendance_regularization_requests_updated_by'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_attendance_regularization_requests')),
    sa.UniqueConstraint('attendance_record_id', 'pending_key', name='uq_att_reg_requests_pending'),
    sa.UniqueConstraint('organization_id', 'id', name='uq_att_reg_requests_org_id')
    )
    op.create_index('ix_att_reg_requests_approver', 'attendance_regularization_requests', ['organization_id', 'status', 'current_approver_user_id'], unique=False)
    op.create_index('ix_att_reg_requests_employee', 'attendance_regularization_requests', ['organization_id', 'employee_id', 'business_date'], unique=False)
    op.create_index(op.f('ix_attendance_regularization_requests_organization_id'), 'attendance_regularization_requests', ['organization_id'], unique=False)
    op.create_table('attendance_regularization_actions',
    sa.Column('organization_id', app.db.types.Uuid7(length=16), nullable=False),
    sa.Column('request_id', app.db.types.Uuid7(length=16), nullable=False),
    sa.Column('action', sa.String(length=20), nullable=False),
    sa.Column('approval_level', sa.SmallInteger(), nullable=True),
    sa.Column('from_status', sa.String(length=20), nullable=True),
    sa.Column('to_status', sa.String(length=20), nullable=True),
    sa.Column('from_approver_type', sa.String(length=10), nullable=True),
    sa.Column('to_approver_type', sa.String(length=10), nullable=True),
    sa.Column('from_approver_user_id', app.db.types.Uuid7(length=16), nullable=True),
    sa.Column('to_approver_user_id', app.db.types.Uuid7(length=16), nullable=True),
    sa.Column('comment', sa.String(length=1000), nullable=True),
    sa.Column('actor_user_id', app.db.types.Uuid7(length=16), nullable=False),
    sa.Column('actor_employee_id', app.db.types.Uuid7(length=16), nullable=True),
    sa.Column('occurred_at', app.db.types.UtcDateTime(), nullable=False),
    sa.Column('id', app.db.types.Uuid7(length=16), nullable=False),
    sa.Column('created_at', app.db.types.UtcDateTime(), server_default=sa.text('CURRENT_TIMESTAMP(6)'), nullable=False),
    sa.Column('created_by', app.db.types.Uuid7(length=16), nullable=True),
    sa.CheckConstraint("action IN ('SUBMITTED','APPROVED_LEVEL_1','APPROVED_FINAL','OVERRIDE_APPROVED','REJECTED','WITHDRAWN','REASSIGNED','CANCELLED')", name='ck_att_reg_actions_action'),
    sa.ForeignKeyConstraint(['actor_user_id'], ['users.id'], name=op.f('fk_attendance_regularization_actions_actor_user_id'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], name=op.f('fk_attendance_regularization_actions_created_by'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['organization_id', 'request_id'], ['attendance_regularization_requests.organization_id', 'attendance_regularization_requests.id'], name='fk_att_reg_actions_request', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], name=op.f('fk_attendance_regularization_actions_organization_id'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_attendance_regularization_actions')),
    sa.UniqueConstraint('organization_id', 'id', name='uq_att_reg_actions_org_id')
    )
    op.create_index('ix_att_reg_actions_request', 'attendance_regularization_actions', ['request_id', 'occurred_at'], unique=False)
    op.create_index(op.f('ix_attendance_regularization_actions_organization_id'), 'attendance_regularization_actions', ['organization_id'], unique=False)
    op.create_table('attendance_regularization_items',
    sa.Column('organization_id', app.db.types.Uuid7(length=16), nullable=False),
    sa.Column('request_id', app.db.types.Uuid7(length=16), nullable=False),
    sa.Column('item_type', sa.String(length=12), nullable=False),
    sa.Column('punch_type', sa.String(length=3), nullable=True),
    sa.Column('punch_at', app.db.types.UtcDateTime(), nullable=True),
    sa.Column('work_arrangement', sa.String(length=6), nullable=True),
    sa.Column('target_punch_id', app.db.types.Uuid7(length=16), nullable=True),
    sa.Column('half', sa.String(length=11), nullable=True),
    sa.Column('created_punch_id', app.db.types.Uuid7(length=16), nullable=True),
    sa.Column('id', app.db.types.Uuid7(length=16), nullable=False),
    sa.Column('created_at', app.db.types.UtcDateTime(), server_default=sa.text('CURRENT_TIMESTAMP(6)'), nullable=False),
    sa.Column('created_by', app.db.types.Uuid7(length=16), nullable=True),
    sa.CheckConstraint("(item_type = 'ADD_PUNCH') = (punch_type IS NOT NULL AND punch_at IS NOT NULL)", name='ck_att_reg_items_add'),
    sa.CheckConstraint("(item_type = 'SET_ON_DUTY') = (half IS NOT NULL)", name='ck_att_reg_items_duty'),
    sa.CheckConstraint("(item_type = 'VOID_PUNCH') = (target_punch_id IS NOT NULL)", name='ck_att_reg_items_void'),
    sa.CheckConstraint("item_type IN ('ADD_PUNCH','VOID_PUNCH','SET_ON_DUTY')", name='ck_att_reg_items_type'),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], name=op.f('fk_attendance_regularization_items_created_by'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['organization_id', 'request_id'], ['attendance_regularization_requests.organization_id', 'attendance_regularization_requests.id'], name='fk_att_reg_items_request', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], name=op.f('fk_attendance_regularization_items_organization_id'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_attendance_regularization_items')),
    sa.UniqueConstraint('organization_id', 'id', name='uq_att_reg_items_org_id')
    )
    op.create_index('ix_att_reg_items_request', 'attendance_regularization_items', ['request_id'], unique=False)
    op.create_index(op.f('ix_attendance_regularization_items_organization_id'), 'attendance_regularization_items', ['organization_id'], unique=False)


def downgrade() -> None:
    # Dropping a table drops its indexes; MySQL refuses to drop an index a foreign key uses.
    op.drop_table('attendance_regularization_actions')
    op.drop_table('attendance_regularization_items')
    op.drop_table('attendance_regularization_requests')
