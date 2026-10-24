"""Registry of every REST resource built from the ORM models.

Adding a table to :mod:`app.models` and a row here automatically exposes the
standard REST surface (list/get/create/patch/delete/bulk/export).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from fastapi import APIRouter

from app.api.factory import build_resource_router
from app.schemas.factory import (
    build_create_schema,
    build_read_schema,
    build_update_schema,
)
from app import models


@dataclass(frozen=True)
class ResourceSpec:
    slug: str
    model: type
    tag: str
    permission: str | None = None
    search_fields: tuple[str, ...] = ()
    default_sort: tuple[str, ...] = ()
    audited: bool = False
    read_only: bool = False
    create_exclude: tuple[str, ...] = ()
    update_exclude: tuple[str, ...] = ()
    create_overrides: dict = field(default_factory=dict)
    update_overrides: dict = field(default_factory=dict)


RESOURCES: list[ResourceSpec] = [
    # ------------------------------------------------------------ Organisation
    ResourceSpec("organizations", models.Organization, "Organisation", "organization",
                 search_fields=("name", "legal_name"), default_sort=("name",), audited=False),
    ResourceSpec("legal-entities", models.LegalEntity, "Organisation", "legal_entity",
                 search_fields=("name", "code"), default_sort=("name",), audited=True),
    ResourceSpec("locations", models.Location, "Organisation", "location",
                 search_fields=("name", "code", "city"), default_sort=("name",)),
    ResourceSpec("departments", models.Department, "Organisation", "department",
                 search_fields=("name", "code"), default_sort=("name",)),
    ResourceSpec("designations", models.Designation, "Organisation", "designation",
                 search_fields=("title", "code"), default_sort=("title",)),
    ResourceSpec("cost-centers", models.CostCenter, "Organisation", "cost_center",
                 search_fields=("name", "code"), default_sort=("name",)),

    # ----------------------------------------------------------------- Access
    ResourceSpec("users", models.User, "Access", "user",
                 search_fields=("email",), default_sort=("email",),
                 create_exclude=("password_hash",),
                 create_overrides={}, update_exclude=("password_hash",)),
    ResourceSpec("roles", models.Role, "Access", "role",
                 search_fields=("name", "code"), default_sort=("name",), audited=True),
    ResourceSpec("permissions", models.Permission, "Access", "permission",
                 search_fields=("code", "module", "action"), default_sort=("code",)),

    # ----------------------------------------------------------------- People
    ResourceSpec("employees", models.Employee, "People", "employee",
                 search_fields=("employee_code", "first_name", "last_name", "email"),
                 default_sort=("employee_code",), audited=True),
    ResourceSpec("employee-job-history", models.EmployeeJobHistory, "People", "employee",
                 search_fields=(), default_sort=("effective_date",)),
    ResourceSpec("employee-bank-details", models.EmployeeBankDetail, "People", "employee",
                 search_fields=("account_name", "bank_name", "account_number"),
                 default_sort=("created_at",), audited=True),
    ResourceSpec("employee-statutory-details", models.EmployeeStatutoryDetail, "People", "employee",
                 search_fields=("pan", "uan", "esic_number"), audited=True),
    ResourceSpec("document-types", models.DocumentType, "People", "document",
                 search_fields=("name", "category"), default_sort=("name",)),
    ResourceSpec("employee-documents", models.EmployeeDocument, "People", "employee_document",
                 search_fields=("file_name", "status"), default_sort=("uploaded_at",)),
    ResourceSpec("document-templates", models.DocumentTemplate, "People", "document",
                 search_fields=("name", "type"), default_sort=("name",)),

    # ---------------------------------------------------------------- Payroll
    ResourceSpec("pay-groups", models.PayGroup, "Payroll", "pay_group",
                 search_fields=("name", "pay_cycle", "currency"), default_sort=("name",), audited=True),
    ResourceSpec("salary-structures", models.SalaryStructure, "Payroll", "salary_structure",
                 search_fields=("name", "code"), default_sort=("name",), audited=True),
    ResourceSpec("salary-components", models.SalaryComponent, "Payroll", "salary_component",
                 search_fields=("name", "code"), default_sort=("name",), audited=True),
    ResourceSpec("salary-structure-components", models.SalaryStructureComponent, "Payroll",
                 "salary_structure", search_fields=(), default_sort=("sequence",)),
    ResourceSpec("employee-salaries", models.EmployeeSalary, "Payroll", "employee_salary",
                 search_fields=("status",), default_sort=("effective_from",), audited=True),
    ResourceSpec("salary-revisions", models.SalaryRevision, "Payroll", "salary_revision",
                 search_fields=("status",), default_sort=("created_at",), audited=True),
    ResourceSpec("payroll-runs", models.PayrollRun, "Payroll", "payroll_run",
                 search_fields=("status", "run_type"), default_sort=("period_start",), audited=True,
                 create_exclude=("status", "approved_by", "finalized_at", "created_by")),
    ResourceSpec("payroll-run-employees", models.PayrollRunEmployee, "Payroll", "payroll_run",
                 search_fields=("status",)),
    ResourceSpec("payslips", models.Payslip, "Payroll", "payslip",
                 search_fields=("status",), default_sort=("period_start",), audited=True,
                 update_exclude=("employee_id", "payroll_run_employee_id")),
    ResourceSpec("payslip-lines", models.PayslipLine, "Payroll", "payslip",
                 search_fields=("component_name", "type")),
    ResourceSpec("statutory-settings", models.StatutorySetting, "Payroll", "statutory",
                 search_fields=("country_code",)),

    # -------------------------------------------------------------------- Tax
    ResourceSpec("tax-declarations", models.TaxDeclaration, "Tax", "tax_declaration",
                 search_fields=("financial_year", "regime", "status"), default_sort=("financial_year",),
                 audited=True),
    ResourceSpec("tax-proofs", models.TaxProof, "Tax", "tax_proof",
                 search_fields=("section", "status")),

    # ---------------------------------------------------------------- Finance
    ResourceSpec("expense-policies", models.ExpensePolicy, "Finance", "expense",
                 search_fields=("name",), default_sort=("name",)),
    ResourceSpec("expense-claims", models.ExpenseClaim, "Finance", "expense",
                 search_fields=("status",), default_sort=("claim_date",), audited=True,
                 create_exclude=("status", "approved_by", "submitted_at")),
    ResourceSpec("expense-claim-lines", models.ExpenseClaimLine, "Finance", "expense",
                 search_fields=("category",)),
    ResourceSpec("loan-policies", models.LoanPolicy, "Finance", "loan",
                 search_fields=("name",), default_sort=("name",)),
    ResourceSpec("loans", models.Loan, "Finance", "loan",
                 search_fields=("status",), default_sort=("status",), audited=True,
                 create_exclude=("status", "disbursed_at")),
    ResourceSpec("loan-repayments", models.LoanRepayment, "Finance", "loan",
                 search_fields=("status",), default_sort=("due_date",)),

    # ------------------------------------------------------------------ Leave
    ResourceSpec("leave-types", models.LeaveType, "Leave", "leave",
                 search_fields=("name", "code"), default_sort=("name",)),
    ResourceSpec("leave-balances", models.LeaveBalance, "Leave", "leave",
                 search_fields=(), default_sort=("employee_id",)),
    ResourceSpec("leave-requests", models.LeaveRequest, "Leave", "leave",
                 search_fields=("status", "reason"), default_sort=("start_date",),
                 create_exclude=("status", "approved_by")),

    # -------------------------------------------------------------- Time
    ResourceSpec("shifts", models.Shift, "Time", "shift",
                 search_fields=("name",), default_sort=("name",)),
    ResourceSpec("holidays", models.Holiday, "Time", "holiday",
                 search_fields=("name",), default_sort=("date",)),
    ResourceSpec("attendance-records", models.AttendanceRecord, "Time", "attendance",
                 search_fields=("status", "source"), default_sort=("date",)),

    # -------------------------------------------------------------- Lifecycle
    ResourceSpec("onboarding-templates", models.OnboardingTemplate, "Lifecycle", "onboarding",
                 search_fields=("name", "role"), default_sort=("name",)),
    ResourceSpec("onboarding-tasks", models.OnboardingTask, "Lifecycle", "onboarding",
                 search_fields=("title", "owner_role"), default_sort=("sequence",)),
    ResourceSpec("employee-onboarding", models.EmployeeOnboarding, "Lifecycle", "onboarding",
                 search_fields=("status",)),
    ResourceSpec("exit-requests", models.ExitRequest, "Lifecycle", "exit",
                 search_fields=("status", "reason"), default_sort=("resignation_date",),
                 create_exclude=("status", "approved_by")),
    ResourceSpec("exit-tasks", models.ExitTask, "Lifecycle", "exit",
                 search_fields=("title", "owner_role", "status")),
    ResourceSpec("assets", models.Asset, "Lifecycle", "asset",
                 search_fields=("asset_code", "name", "category", "serial_number", "status"),
                 default_sort=("asset_code",)),
    ResourceSpec("asset-assignments", models.AssetAssignment, "Lifecycle", "asset",
                 search_fields=("status",), default_sort=("assigned_date",)),

    # -------------------------------------------------------------- Workflows
    ResourceSpec("helpdesk-tickets", models.HelpdeskTicket, "Support", "helpdesk",
                 search_fields=("category", "subject", "status"), default_sort=("sla_due_at",)),
    ResourceSpec("workflows", models.Workflow, "Support", "workflow",
                 search_fields=("name", "module", "trigger_event"), default_sort=("name",), audited=True),
    ResourceSpec("workflow-steps", models.WorkflowStep, "Support", "workflow",
                 search_fields=("approver_role",), default_sort=("step_order",)),
    ResourceSpec("approvals", models.Approval, "Support", "approval",
                 search_fields=("entity_type", "status"), default_sort=("step_order",)),
    ResourceSpec("notifications", models.Notification, "Support", "notification",
                 search_fields=("title", "channel", "status"), default_sort=("sent_at",)),
    ResourceSpec("audit-logs", models.AuditLog, "Admin", "audit",
                 search_fields=("entity_type", "action"), default_sort=("created_at",),
                 read_only=True),

    # --------------------------------------------------------------- Currency
    ResourceSpec("currency-rates", models.CurrencyRate, "Admin", "currency",
                 search_fields=("base_currency", "quote_currency", "source"),
                 default_sort=("effective_date",), audited=True),
    ResourceSpec("display-currency-preferences", models.DisplayCurrencyPreference, "Admin", "currency",
                 search_fields=(), audited=True),
    ResourceSpec("integrations", models.Integration, "Admin", "integration",
                 search_fields=("provider", "status"), default_sort=("provider",)),
]


def _camel(slug: str) -> str:
    return "".join(part.capitalize() for part in slug.replace("_", "-").split("-"))


def build_all_resource_routers() -> list[APIRouter]:
    routers: list[APIRouter] = []
    for spec in RESOURCES:
        name = _camel(spec.slug)
        read_schema = build_read_schema(spec.model, f"{name}Read")
        create_schema = None
        update_schema = None
        if not spec.read_only:
            create_schema = build_create_schema(
                spec.model,
                f"{name}Create",
                exclude=set(spec.create_exclude),
                overrides=spec.create_overrides,
            )
            update_schema = build_update_schema(
                spec.model,
                f"{name}Update",
                exclude=set(spec.update_exclude),
                overrides=spec.update_overrides,
            )
        routers.append(
            build_resource_router(
                slug=spec.slug,
                model=spec.model,
                tag=spec.tag,
                read_schema=read_schema,
                create_schema=create_schema,
                update_schema=update_schema,
                search_fields=spec.search_fields,
                default_sort=spec.default_sort,
                permission=spec.permission,
                audited=spec.audited,
                read_only=spec.read_only,
            )
        )
    return routers
