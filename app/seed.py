"""Idempotent database seeding.

Creates baseline permissions, a global-admin user and a small demo data set.

Can be called programmatically (``run_seed``) or via the CLI wrappers:

    python -m app.seed            # from backend/
    python scripts/seed.py        # from backend/
    python seed.py                # from backend/
    python backend/seed.py        # from the repo root
"""
from __future__ import annotations

from datetime import date, time, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.registry import RESOURCES
from app.core.config import settings
from app.core.database import Base, SessionLocal, engine, init_db
from app.core.security import hash_password
from app.models import (
    CostCenter,
    Department,
    Designation,
    DocumentType,
    Employee,
    ExpensePolicy,
    LeaveType,
    LegalEntity,
    Location,
    LoanPolicy,
    Organization,
    PayGroup,
    SalaryComponent,
    SalaryStructure,
    SalaryStructureComponent,
    Shift,
)
from app.models.auth import Permission, Role, RolePermission, User, UserRole

ACTIONS = ("view", "create", "edit", "delete", "bulk", "approve")

_DEMO_TABLES = (
    "organizations",
    "users",
    "permissions",
    "employees",
    "departments",
    "designations",
    "pay_groups",
    "salary_structures",
    "salary_components",
    "leave_types",
    "expense_policies",
    "loan_policies",
)


def database_is_empty(db: Session) -> bool:
    """True when the database has no organisations and no users yet."""
    orgs = db.execute(select(func.count()).select_from(Organization)).scalar_one()
    users = db.execute(select(func.count()).select_from(User)).scalar_one()
    return int(orgs) == 0 and int(users) == 0


def seed_permissions(db: Session) -> dict[str, Permission]:
    modules = {spec.permission for spec in RESOURCES if spec.permission}
    modules |= {
        "payroll_run",
        "payslip",
        "employee_salary",
        "salary_revision",
        "attendance",
        "employee_document",
        "tax_declaration",
        "tax_proof",
        "audit",
        "currency",
        "user",
        "role",
        "permission",
        "report",
        "settings",
    }
    created: dict[str, Permission] = {}
    for module in sorted(modules):
        for action in ACTIONS:
            code = f"{module}.{action}"
            existing = db.execute(
                select(Permission).where(Permission.code == code)
            ).scalar_one_or_none()
            if existing is None:
                existing = Permission(
                    code=code, module=module, action=action, description=f"{action} {module}"
                )
                db.add(existing)
                db.flush()
            created[code] = existing
    return created


def get_or_create(db: Session, model, defaults: dict, **filters):
    instance = db.execute(select(model).filter_by(**filters)).scalar_one_or_none()
    if instance is None:
        instance = model(**{**filters, **defaults})
        db.add(instance)
        db.flush()
    return instance


def _seed_demo(db: Session) -> None:
    permissions = seed_permissions(db)

    org = get_or_create(
        db,
        Organization,
        {"legal_name": "Acme Corporation Pvt Ltd", "country_code": "IN",
         "default_currency": "INR", "timezone": "Asia/Kolkata"},
        name="Acme Corp",
    )
    admin_role = get_or_create(
        db,
        Role,
        {"name": "Global Admin", "is_system": True, "org_id": org.id,
         "description": "Full unrestricted access"},
        code="global_admin",
    )
    existing_links = {
        link.permission_id
        for link in db.execute(
            select(RolePermission).where(RolePermission.role_id == admin_role.id)
        ).scalars().all()
    }
    for permission in permissions.values():
        if permission.id not in existing_links:
            db.add(RolePermission(role_id=admin_role.id, permission_id=permission.id))
    db.flush()

    admin = db.execute(
        select(User).where(User.email == settings.seed_admin_email)
    ).scalar_one_or_none()
    if admin is None:
        admin = User(
            email=settings.seed_admin_email,
            password_hash=hash_password(settings.seed_admin_password),
            is_active=True,
        )
        db.add(admin)
        db.flush()
    if not any(role.code == "global_admin" for role in admin.roles):
        db.add(UserRole(user_id=admin.id, role_id=admin_role.id))
    db.flush()

    entity = get_or_create(
        db,
        LegalEntity,
        {"org_id": org.id, "country_code": "IN", "base_currency": "INR",
         "pf_code": "PF/ACME/001", "esic_code": "ESIC/ACME/001", "tan": "ACMEP1234F"},
        name="Acme India", code="ACME-IN",
    )
    location = get_or_create(
        db,
        Location,
        {"legal_entity_id": entity.id, "name": "Bengaluru HQ", "code": "BLR",
         "address": "1 MG Road", "city": "Bengaluru", "state": "Karnataka",
         "country_code": "IN", "timezone": "Asia/Kolkata"},
        name="Bengaluru HQ", code="BLR",
    )
    engineering = get_or_create(
        db, Department, {"org_id": org.id, "code": "ENG"},
        name="Engineering", org_id=org.id,
    )
    get_or_create(
        db, Department, {"org_id": org.id, "code": "HR"}, name="Human Resources", org_id=org.id
    )
    engineer = get_or_create(
        db, Designation, {"org_id": org.id, "code": "SE", "grade": "L3"},
        title="Software Engineer", org_id=org.id,
    )
    get_or_create(
        db, Designation, {"org_id": org.id, "code": "EM", "grade": "L5"},
        title="Engineering Manager", org_id=org.id,
    )
    cost_center = get_or_create(
        db, CostCenter, {"org_id": org.id, "legal_entity_id": entity.id, "code": "CC-ENG"},
        name="Engineering Cost Center", org_id=org.id, legal_entity_id=entity.id,
    )
    pay_group = get_or_create(
        db, PayGroup,
        {"org_id": org.id, "legal_entity_id": entity.id, "pay_cycle": "monthly",
         "cut_off_day": 25, "pay_date": 1, "currency": "INR"},
        name="India Monthly", legal_entity_id=entity.id,
    )
    get_or_create(
        db, Shift,
        {"org_id": org.id, "start_time": time(9, 0), "end_time": time(18, 0),
         "break_minutes": 60},
        name="General Shift", org_id=org.id,
    )
    get_or_create(
        db, LeaveType,
        {"org_id": org.id, "code": "EL", "accrual_type": "monthly",
         "max_balance": Decimal("30"), "encashable": True, "is_paid": True},
        name="Earned Leave", org_id=org.id,
    )
    get_or_create(
        db, DocumentType, {"org_id": org.id, "category": "Identity", "is_mandatory": True},
        name="PAN Card", org_id=org.id,
    )
    get_or_create(
        db, ExpensePolicy,
        {"org_id": org.id, "currency": "INR", "max_amount": Decimal("50000"),
         "requires_receipt": True, "is_active": True},
        name="Standard Expense Policy", org_id=org.id,
    )
    get_or_create(
        db, LoanPolicy,
        {"org_id": org.id, "interest_rate": Decimal("8"), "max_amount": Decimal("500000"),
         "max_tenure_months": 36, "is_active": True},
        name="Standard Loan", org_id=org.id,
    )

    structure = get_or_create(
        db, SalaryStructure, {"org_id": org.id, "code": "STD-2026", "is_active": True},
        name="Standard Structure 2026", org_id=org.id,
    )
    basic = get_or_create(
        db, SalaryComponent,
        {"org_id": org.id, "component_type": "earning", "calculation_type": "percentage",
         "formula": "50%", "taxable": True, "is_active": True},
        name="Basic", org_id=org.id, code="BASIC",
    )
    hra = get_or_create(
        db, SalaryComponent,
        {"org_id": org.id, "component_type": "earning", "calculation_type": "percentage",
         "formula": "20%", "taxable": True, "is_active": True},
        name="House Rent Allowance", org_id=org.id, code="HRA",
    )
    pf = get_or_create(
        db, SalaryComponent,
        {"org_id": org.id, "component_type": "deduction", "calculation_type": "percentage",
         "formula": "12%", "taxable": False, "is_statutory": True, "is_active": True},
        name="Provident Fund", org_id=org.id, code="PF",
    )
    for sequence, component in enumerate((basic, hra, pf)):
        link = db.execute(
            select(SalaryStructureComponent).where(
                SalaryStructureComponent.structure_id == structure.id,
                SalaryStructureComponent.component_id == component.id,
            )
        ).scalar_one_or_none()
        if link is None:
            db.add(
                SalaryStructureComponent(
                    structure_id=structure.id,
                    component_id=component.id,
                    sequence=sequence,
                    is_mandatory=True,
                    default_value=None,
                )
            )

    employee = db.execute(
        select(Employee).where(Employee.employee_code == "EMP0001")
    ).scalar_one_or_none()
    if employee is None:
        db.add(
            Employee(
                org_id=org.id,
                employee_code="EMP0001",
                first_name="Asha",
                last_name="Rao",
                email="asha.rao@example.com",
                phone="+919999999999",
                date_of_birth=date(1992, 5, 12),
                date_of_joining=date.today() - timedelta(days=400),
                employment_type="full_time",
                status="active",
                legal_entity_id=entity.id,
                department_id=engineering.id,
                designation_id=engineer.id,
                location_id=location.id,
                cost_center_id=cost_center.id,
                pay_group_id=pay_group.id,
            )
        )


def run_seed(*, reset: bool = False) -> dict:
    """Seed the database and return a summary of row counts.

    ``reset=True`` drops every table first (destructive) to give a clean seed.
    """
    init_db()
    if reset:
        Base.metadata.drop_all(bind=engine)
        init_db()

    db = SessionLocal()
    try:
        _seed_demo(db)
        db.commit()
        return _counts(db)
    finally:
        db.close()


def _counts(db: Session) -> dict:
    summary: dict[str, int] = {}
    for table_name in _DEMO_TABLES:
        table = Base.metadata.tables.get(table_name)
        if table is not None:
            summary[table_name] = int(
                db.execute(select(func.count()).select_from(table)).scalar_one()
            )
    return summary


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Seed baseline + demo data.")
    parser.add_argument(
        "--reset", action="store_true", help="Drop all tables first (destructive)."
    )
    args = parser.parse_args()

    if args.reset:
        print("!! --reset: dropping all tables and recreating the schema ...")
    summary = run_seed(reset=args.reset)
    print("Seed complete.")
    for table_name, count in summary.items():
        print(f"  {table_name:22} {count}")
    print(f"Admin login: {settings.seed_admin_email} / {settings.seed_admin_password}")


if __name__ == "__main__":
    main()
