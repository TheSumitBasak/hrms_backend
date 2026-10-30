"""onboarding attendance leave foundation

Revision ID: b7e31f4a9c10
Revises: c4a71e5d9f08
Create Date: 2026-10-29 12:00:00.000000

Shared groundwork for the onboarding / attendance / leave addendum (sections 9 and 10): fifteen
``organization_settings`` columns with their CHECKs, the ``ATTENDANCE_SOURCE`` lookup list,
the OTO and AIC roles, and the 34 new permission codes with their ``Y`` role cells. ``C`` cells
are per-organization opt-ins (DEC-004) and are not seeded. Idempotent seeds, like the JA one.

The three seeded document categories (addendum 9.3) are created per organization by
``app.cli seed``; this migration deliberately does not duplicate that logic in SQL.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "b7e31f4a9c10"
down_revision: Union[str, None] = "c4a71e5d9f08"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

PERMISSIONS: tuple[str, ...] = ('onboarding.template_manage', 'onboarding.view', 'onboarding.initiate', 'onboarding.manage', 'onboarding.task_execute', 'onboarding.task_review', 'onboarding.task_waive', 'onboarding.complete', 'onboarding.cancel', 'onboarding.reopen', 'onboarding.self_access_manage', 'holiday.manage', 'attendance.policy_manage', 'attendance.shift_assign', 'attendance.view', 'attendance.punch_manage', 'attendance.import', 'attendance.finalize', 'attendance.correct', 'attendance.regularize_approve', 'attendance.regularize_approve_hr', 'leave.type_manage', 'leave.view', 'leave.balance_view', 'leave.apply_on_behalf', 'leave.approve', 'leave.approve_hr', 'leave.cancel_any', 'leave.balance_adjust', 'leave.admin_correct', 'leave.process_run', 'self.onboarding', 'self.attendance', 'self.leave')

ROLES: tuple[tuple[str, str, str], ...] = (
    ("OTO", "Onboarding Task Owner", "Executes or reviews assigned onboarding tasks."),
    ("AIC", "Attendance Integration Client", "Service account that imports punches."),
)

ROLE_GRANTS: dict[str, tuple[str, ...]] = {'OA': ('attendance.policy_manage', 'attendance.shift_assign', 'holiday.manage', 'leave.process_run', 'leave.type_manage', 'onboarding.self_access_manage', 'onboarding.template_manage', 'onboarding.view'), 'HRA': ('attendance.correct', 'attendance.finalize', 'attendance.policy_manage', 'attendance.punch_manage', 'attendance.regularize_approve', 'attendance.regularize_approve_hr', 'attendance.shift_assign', 'attendance.view', 'holiday.manage', 'leave.admin_correct', 'leave.apply_on_behalf', 'leave.approve', 'leave.approve_hr', 'leave.balance_adjust', 'leave.balance_view', 'leave.cancel_any', 'leave.process_run', 'leave.type_manage', 'leave.view', 'onboarding.cancel', 'onboarding.complete', 'onboarding.initiate', 'onboarding.manage', 'onboarding.reopen', 'onboarding.self_access_manage', 'onboarding.task_execute', 'onboarding.task_review', 'onboarding.task_waive', 'onboarding.template_manage', 'onboarding.view'), 'HRE': ('attendance.punch_manage', 'attendance.regularize_approve', 'attendance.shift_assign', 'attendance.view', 'leave.apply_on_behalf', 'leave.approve', 'leave.balance_view', 'leave.view', 'onboarding.complete', 'onboarding.initiate', 'onboarding.manage', 'onboarding.task_execute', 'onboarding.task_review', 'onboarding.view'), 'DM': ('attendance.regularize_approve', 'attendance.view', 'leave.approve', 'leave.view', 'onboarding.task_execute', 'onboarding.task_review', 'onboarding.view'), 'RDR': ('onboarding.view',), 'OTO': ('onboarding.task_execute', 'onboarding.task_review', 'onboarding.view'), 'AIC': ('attendance.import',)}

_LISTS_BEFORE = "'GENDER','MARITAL_STATUS','BLOOD_GROUP','RELATIONSHIP','QUALIFICATION_LEVEL','STUDY_MODE','SOURCE_OF_HIRE','DISABILITY_TYPE','BANK_ACCOUNT_TYPE','INDUSTRY','BGV_STATUS','STATUS_REASON','JOB_CHANGE_TYPE','SALARY_REVISION_REASON','DOCUMENT_TYPE','RELIGION','SOCIAL_CATEGORY','COMPETENCY_CATEGORY'"

_BOOLEANS = (
    "enable_onboarding_module",
    "enable_attendance_module",
    "enable_leave_module",
    "onboarding_required_for_activation",
    "onboarding_auto_complete",
    "enable_onboarding_self_service",
    "enable_attendance_self_service",
    "enable_leave_self_service",
)

_CHECKS = (
    ("ck_organization_settings_leave_year_month", "leave_year_start_month BETWEEN 1 AND 12"),
    ("ck_organization_settings_onb_reopen_window", "onboarding_reopen_window_days BETWEEN 0 AND 365"),
    (
        "ck_organization_settings_onb_payload_retention",
        "onboarding_submission_payload_retention_days BETWEEN 1 AND 365",
    ),
    (
        "ck_organization_settings_att_start_required",
        "NOT enable_attendance_module OR attendance_tracking_start_date IS NOT NULL",
    ),
    (
        "ck_organization_settings_leave_start_required",
        "NOT enable_leave_module OR leave_tracking_start_date IS NOT NULL",
    ),
)


def _small_int(name: str, default: int) -> sa.Column:
    return sa.Column(name, sa.SmallInteger(), server_default=sa.text(str(default)), nullable=False)


def upgrade() -> None:
    for name in _BOOLEANS:
        op.add_column(
            "organization_settings",
            sa.Column(name, sa.Boolean(), server_default=sa.text("0"), nullable=False),
        )
    op.add_column("organization_settings", sa.Column("attendance_tracking_start_date", sa.Date()))
    op.add_column("organization_settings", sa.Column("leave_tracking_start_date", sa.Date()))
    op.add_column("organization_settings", _small_int("leave_year_start_month", 1))
    op.add_column("organization_settings", _small_int("onboarding_reopen_window_days", 30))
    op.add_column(
        "organization_settings", _small_int("onboarding_submission_payload_retention_days", 30)
    )
    op.add_column(
        "organization_settings", sa.Column("default_weekly_off_pattern_id", mysql.BINARY(16))
    )
    op.add_column(
        "organization_settings",
        sa.Column(
            "attendance_day_close_time",
            sa.Time(),
            server_default=sa.text("'12:00:00'"),
            nullable=False,
        ),
    )
    for name, condition in _CHECKS:
        op.create_check_constraint(name, "organization_settings", condition)

    op.drop_constraint("ck_lookup_values_list_code", "lookup_values", type_="check")
    op.create_check_constraint(
        "ck_lookup_values_list_code",
        "lookup_values",
        f"list_code IN ({_LISTS_BEFORE},'ATTENDANCE_SOURCE')",
    )

    for code, name, description in ROLES:
        op.execute(
            "INSERT INTO roles (id, role_code, name, description, is_system) "
            f"SELECT UNHEX(REPLACE(UUID(),'-','')), '{code}', '{name}', '{description}', 1 "
            f"FROM DUAL WHERE NOT EXISTS (SELECT 1 FROM roles WHERE role_code='{code}')"
        )
    for code in PERMISSIONS:
        op.execute(
            "INSERT INTO permissions (id, permission_code, description) "
            f"SELECT UNHEX(REPLACE(UUID(),'-','')), '{code}', 'Addendum permission {code}' "
            f"FROM DUAL WHERE NOT EXISTS "
            f"(SELECT 1 FROM permissions WHERE permission_code='{code}')"
        )
    for role_code, codes in ROLE_GRANTS.items():
        in_list = ",".join(f"'{c}'" for c in codes)
        op.execute(
            "INSERT INTO role_permissions (role_id, permission_id) "
            "SELECT r.id, p.id FROM roles r "
            f"JOIN permissions p ON p.permission_code IN ({in_list}) "
            f"WHERE r.role_code='{role_code}' AND NOT EXISTS ("
            "SELECT 1 FROM role_permissions rp "
            "WHERE rp.role_id=r.id AND rp.permission_id=p.id)"
        )


def downgrade() -> None:
    in_list = ",".join(f"'{c}'" for c in PERMISSIONS)
    op.execute(
        "DELETE rp FROM role_permissions rp JOIN permissions p ON p.id=rp.permission_id "
        f"WHERE p.permission_code IN ({in_list})"
    )
    op.execute(f"DELETE FROM organization_role_permissions WHERE permission_code IN ({in_list})")
    op.execute(f"DELETE FROM permissions WHERE permission_code IN ({in_list})")
    op.execute(
        "DELETE rp FROM role_permissions rp JOIN roles r ON r.id=rp.role_id "
        "WHERE r.role_code IN ('OTO','AIC')"
    )
    op.execute("DELETE FROM roles WHERE role_code IN ('OTO','AIC')")

    op.execute("DELETE FROM lookup_values WHERE list_code = 'ATTENDANCE_SOURCE'")
    op.drop_constraint("ck_lookup_values_list_code", "lookup_values", type_="check")
    op.create_check_constraint(
        "ck_lookup_values_list_code", "lookup_values", f"list_code IN ({_LISTS_BEFORE})"
    )
    for name, _ in reversed(_CHECKS):
        op.drop_constraint(name, "organization_settings", type_="check")
    for column in (
        "attendance_day_close_time",
        "default_weekly_off_pattern_id",
        "onboarding_submission_payload_retention_days",
        "onboarding_reopen_window_days",
        "leave_year_start_month",
        "leave_tracking_start_date",
        "attendance_tracking_start_date",
        *reversed(_BOOLEANS),
    ):
        op.drop_column("organization_settings", column)
