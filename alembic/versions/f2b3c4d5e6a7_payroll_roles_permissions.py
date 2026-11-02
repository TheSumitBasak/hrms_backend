"""payroll roles and permissions; widen role_code to 20 for SELF_ALUMNI

Revision ID: f2b3c4d5e6a7
Revises: e1a2b3c4d5f6
Create Date: 2026-11-02 12:00:00.000000

Payroll addendum 16.1 to 16.3. The 7 new roles, the permission codes and the ``Y`` cells of
the role matrix. ``C`` cells stay per-organization opt-ins (DEC-004) and are not linked.

Idempotent like the earlier permission revisions: every insert is guarded by NOT EXISTS, so
it can run where ``app.cli seed`` has already created the rows from the code matrix.
"""
# ruff: noqa: S608 - every interpolated value is a constant defined in this file

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f2b3c4d5e6a7"
down_revision: str | None = "e1a2b3c4d5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ROLES: tuple[tuple[str, str, str], ...] = (
    (
        "PAD",
        "Payroll Administrator",
        "Payroll configuration, pay groups, component behaviour, payslip templates and reports.",
    ),
    (
        "PPR",
        "Payroll Processor",
        "Inputs, run creation, calculation, submission, payslip operations and F&F preparation.",
    ),
    (
        "PAPV",
        "Payroll Approver",
        "Reviews, approves, locks and reopens payroll runs and approves F&F.",
    ),
    (
        "FIN",
        "Finance User",
        "Payments, bank advice files, loan disbursement and challan payment recording.",
    ),
    (
        "SCO",
        "Statutory Compliance Officer",
        "Statutory registration settings, challans, returns, reconciliation and certificates.",
    ),
    (
        "PAUD",
        "Payroll Auditor",
        "Read-only access to payroll, tax, statutory and audit data. No reveal.",
    ),
    (
        "SELF_ALUMNI",
        "Employee alumni",
        "Restricted self-service access to the employee's own payslips and tax documents.",
    ),
)

PERMISSIONS: tuple[str, ...] = (
    "payroll.view",
    "payroll.config_manage",
    "payroll.profile_manage",
    "payroll.input_manage",
    "payroll.input_approve",
    "payroll.run_create",
    "payroll.calculate",
    "payroll.review",
    "payroll.approve",
    "payroll.lock",
    "payroll.reopen",
    "payroll.register_view",
    "payroll.amount_reveal",
    "payroll.report_view",
    "payroll.payment_view",
    "payroll.payment_manage",
    "salary_structure.manage",
    "arrear.view",
    "arrear.manage",
    "arrear.approve_recovery",
    "loan.view",
    "loan.config_manage",
    "loan.manage",
    "loan.approve",
    "loan.approve_finance",
    "loan.disburse",
    "tax.view",
    "tax.config_manage",
    "tax.declaration_manage",
    "tax.proof_verify",
    "tax.return_manage",
    "tax.certificate_manage",
    "statutory.config_manage",
    "statutory.rule_prepare",
    "statutory.rule_publish",
    "pf.view",
    "pf.manage",
    "esi.view",
    "esi.manage",
    "pt.view",
    "pt.manage",
    "lwf.view",
    "lwf.manage",
    "statutory.challan_view",
    "statutory.challan_manage",
    "statutory.return_view",
    "statutory.return_manage",
    "statutory.reconcile",
    "bonus.manage",
    "payslip.view",
    "payslip.generate",
    "payslip.reissue",
    "fnf.view",
    "fnf.manage",
    "fnf.approve",
    "fnf.payment",
    "self_access.manage",
    "self.payroll",
    "self.payslip",
    "self.tax",
    "self.tax_documents",
    "self.loan",
)

# Role -> permission codes (Y cells of the section 16.3 matrix, plus audit.view).
ROLE_GRANTS: dict[str, tuple[str, ...]] = {
    "PAD": (
        "arrear.manage",
        "arrear.view",
        "audit.view",
        "bonus.manage",
        "esi.view",
        "fnf.view",
        "loan.manage",
        "loan.view",
        "lwf.view",
        "payroll.amount_reveal",
        "payroll.config_manage",
        "payroll.input_approve",
        "payroll.payment_view",
        "payroll.profile_manage",
        "payroll.register_view",
        "payroll.report_view",
        "payroll.review",
        "payroll.view",
        "payslip.generate",
        "payslip.reissue",
        "payslip.view",
        "pf.view",
        "pt.view",
        "salary_structure.manage",
        "self_access.manage",
        "statutory.challan_view",
        "statutory.config_manage",
        "statutory.return_view",
        "tax.config_manage",
        "tax.declaration_manage",
        "tax.proof_verify",
        "tax.view",
    ),
    "PPR": (
        "arrear.manage",
        "arrear.view",
        "bonus.manage",
        "esi.view",
        "fnf.manage",
        "fnf.view",
        "loan.manage",
        "loan.view",
        "lwf.view",
        "payroll.amount_reveal",
        "payroll.calculate",
        "payroll.input_manage",
        "payroll.payment_view",
        "payroll.profile_manage",
        "payroll.register_view",
        "payroll.report_view",
        "payroll.run_create",
        "payroll.view",
        "payslip.generate",
        "payslip.reissue",
        "payslip.view",
        "pf.view",
        "pt.view",
        "tax.declaration_manage",
        "tax.proof_verify",
        "tax.view",
    ),
    "PAPV": (
        "arrear.approve_recovery",
        "arrear.view",
        "audit.view",
        "esi.view",
        "fnf.approve",
        "fnf.view",
        "loan.approve",
        "loan.manage",
        "loan.view",
        "lwf.view",
        "payroll.amount_reveal",
        "payroll.approve",
        "payroll.input_approve",
        "payroll.lock",
        "payroll.payment_view",
        "payroll.register_view",
        "payroll.reopen",
        "payroll.report_view",
        "payroll.review",
        "payroll.view",
        "payslip.view",
        "pf.view",
        "pt.view",
        "statutory.challan_view",
        "statutory.return_view",
        "tax.view",
    ),
    "FIN": (
        "fnf.payment",
        "fnf.view",
        "loan.approve_finance",
        "loan.disburse",
        "loan.manage",
        "loan.view",
        "payroll.payment_manage",
        "payroll.payment_view",
        "payroll.report_view",
        "payroll.view",
        "statutory.challan_view",
    ),
    "SCO": (
        "audit.view",
        "esi.manage",
        "esi.view",
        "lwf.manage",
        "lwf.view",
        "payroll.report_view",
        "payroll.view",
        "pf.manage",
        "pf.view",
        "pt.manage",
        "pt.view",
        "statutory.challan_manage",
        "statutory.challan_view",
        "statutory.config_manage",
        "statutory.reconcile",
        "statutory.return_manage",
        "statutory.return_view",
        "tax.certificate_manage",
        "tax.config_manage",
        "tax.proof_verify",
        "tax.return_manage",
        "tax.view",
    ),
    "PAUD": (
        "arrear.view",
        "audit.view",
        "esi.view",
        "fnf.view",
        "loan.view",
        "lwf.view",
        "payroll.payment_view",
        "payroll.register_view",
        "payroll.report_view",
        "payroll.view",
        "payslip.view",
        "pf.view",
        "pt.view",
        "statutory.challan_view",
        "statutory.return_view",
        "tax.view",
    ),
    "SSA": ("statutory.rule_prepare", "statutory.rule_publish"),
    "OA": ("self_access.manage",),
    "HRA": ("self_access.manage",),
    "SELF_ALUMNI": ("self.payslip", "self.tax_documents"),
}


def upgrade() -> None:
    op.drop_constraint("ck_roles_role_code_length", "roles", type_="check")
    op.alter_column(
        "roles",
        "role_code",
        existing_type=sa.String(10),
        type_=sa.String(20),
        existing_nullable=False,
    )
    op.create_check_constraint(
        "ck_roles_role_code_length", "roles", "CHAR_LENGTH(role_code) BETWEEN 2 AND 20"
    )
    op.alter_column(
        "organization_role_permissions",
        "role_code",
        existing_type=sa.String(10),
        type_=sa.String(20),
        existing_nullable=False,
    )

    for code, name, description in ROLES:
        name, description = name.replace("'", "''"), description.replace("'", "''")
        op.execute(
            "INSERT INTO roles (id, role_code, name, description, is_system) "
            f"SELECT UNHEX(REPLACE(UUID(),'-','')), '{code}', '{name}', '{description}', 1 "
            f"FROM DUAL WHERE NOT EXISTS (SELECT 1 FROM roles WHERE role_code='{code}')"
        )
    for code in PERMISSIONS:
        op.execute(
            "INSERT INTO permissions (id, permission_code, description) "
            f"SELECT UNHEX(REPLACE(UUID(),'-','')), '{code}', 'Payroll permission {code}' "
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
    role_list = ",".join(f"'{code}'" for code, _, _ in ROLES)
    perm_list = ",".join(f"'{code}'" for code in PERMISSIONS)
    op.execute(
        "DELETE rp FROM role_permissions rp JOIN roles r ON r.id=rp.role_id "
        f"WHERE r.role_code IN ({role_list})"
    )
    op.execute(
        "DELETE rp FROM role_permissions rp JOIN permissions p ON p.id=rp.permission_id "
        f"WHERE p.permission_code IN ({perm_list})"
    )
    op.execute(f"DELETE FROM organization_role_permissions WHERE permission_code IN ({perm_list})")
    op.execute(f"DELETE FROM permissions WHERE permission_code IN ({perm_list})")
    op.execute(
        f"DELETE FROM user_organization_access WHERE role_id IN "
        f"(SELECT id FROM roles WHERE role_code IN ({role_list}))"
    )
    op.execute(f"DELETE FROM roles WHERE role_code IN ({role_list})")
    op.drop_constraint("ck_roles_role_code_length", "roles", type_="check")
    op.alter_column(
        "organization_role_permissions",
        "role_code",
        existing_type=sa.String(20),
        type_=sa.String(10),
        existing_nullable=False,
    )
    op.alter_column(
        "roles",
        "role_code",
        existing_type=sa.String(20),
        type_=sa.String(10),
        existing_nullable=False,
    )
    op.create_check_constraint(
        "ck_roles_role_code_length", "roles", "CHAR_LENGTH(role_code) BETWEEN 2 AND 10"
    )
