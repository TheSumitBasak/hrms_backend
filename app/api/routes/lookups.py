"""Lookup pickers, employee search and global search."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_current_user
from app.core.errors import NotFoundError
from app.crud.base import jsonable
from app.models.auth import Role, User
from app.models.employee import DocumentType, Employee
from app.models.expense import ExpensePolicy
from app.models.leave import LeaveType
from app.models.loan import LoanPolicy
from app.models.organization import (
    CostCenter,
    Department,
    Designation,
    LegalEntity,
    Location,
)
from app.models.payroll import PayGroup

router = APIRouter(tags=["Search"])


def _label(model, obj) -> tuple[str, str | None]:
    if model is Employee:
        return f"{obj.first_name} {obj.last_name}".strip(), obj.employee_code
    if model is User:
        return obj.email, None
    if model is Role:
        return obj.name, obj.code
    if model is LegalEntity:
        return obj.name, obj.code
    if model is Department:
        return obj.name, obj.code
    if model is Designation:
        return obj.title, obj.code
    if model is Location:
        return obj.name, obj.code
    if model is CostCenter:
        return obj.name, obj.code
    if model is PayGroup:
        return obj.name, obj.pay_cycle
    if model is ExpensePolicy:
        return obj.name, obj.currency
    if model is LoanPolicy:
        return obj.name, None
    if model is LeaveType:
        return obj.name, obj.code
    if model is DocumentType:
        return obj.name, obj.category
    return getattr(obj, "name", str(obj.id)), None


LOOKUPS = {
    "employees": (Employee, ("first_name", "last_name", "employee_code", "email")),
    "users": (User, ("email",)),
    "roles": (Role, ("name", "code")),
    "departments": (Department, ("name", "code")),
    "designations": (Designation, ("title", "code")),
    "locations": (Location, ("name", "code", "city")),
    "legal-entities": (LegalEntity, ("name", "code")),
    "cost-centers": (CostCenter, ("name", "code")),
    "pay-groups": (PayGroup, ("name",)),
    "expense-policies": (ExpensePolicy, ("name",)),
    "loan-policies": (LoanPolicy, ("name",)),
    "leave-types": (LeaveType, ("name", "code")),
    "document-types": (DocumentType, ("name", "category")),
}


def _query_lookup(db: Session, model, search_fields, q: str | None, limit: int) -> list:
    stmt = select(model)
    if q:
        conditions = [getattr(model, field).ilike(f"%{q}%") for field in search_fields]
        active = getattr(model, "is_active", None)
        if active is not None and model is not Employee:
            stmt = stmt.where(or_(*conditions))
        else:
            stmt = stmt.where(or_(*conditions))
    return db.execute(stmt.limit(limit)).scalars().all()


@router.get("/lookups/{lookup_type}", summary="Generic FK lookup for pickers")
def lookup(
    lookup_type: str,
    q: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> dict:
    entry = LOOKUPS.get(lookup_type)
    if entry is None:
        raise NotFoundError("Lookup", lookup_type)
    model, search_fields = entry
    rows = _query_lookup(db, model, search_fields, q, limit)
    data = []
    for obj in rows:
        label, sublabel = _label(model, obj)
        data.append({"id": str(obj.id), "label": label, "sublabel": sublabel})
    return {"data": data, "meta": {"total": len(data)}, "errors": None}


@router.get("/employees/search", summary="Typeahead search for employees")
def employees_search(
    q: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> dict:
    stmt = select(Employee)
    if q:
        stmt = stmt.where(
            or_(
                Employee.first_name.ilike(f"%{q}%"),
                Employee.last_name.ilike(f"%{q}%"),
                Employee.employee_code.ilike(f"%{q}%"),
                Employee.email.ilike(f"%{q}%"),
            )
        )
    rows = db.execute(stmt.limit(limit)).scalars().all()
    return {
        "data": [
            {
                "id": str(e.id),
                "label": f"{e.first_name} {e.last_name}".strip(),
                "sublabel": e.employee_code,
                "email": e.email,
                "status": e.status,
            }
            for e in rows
        ],
        "meta": {"total": len(rows)},
        "errors": None,
    }


@router.get("/search", summary="Global search across employees, documents, pages and reports")
def global_search(
    q: str = Query("", min_length=0),
    limit: int = Query(10, ge=1, le=50),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> dict:
    from app.api.routes.analytics import REPORTS
    from app.models.employee import EmployeeDocument

    term = q.strip()
    employees = []
    documents = []
    if term:
        employees = db.execute(
            select(Employee)
            .where(
                or_(
                    Employee.first_name.ilike(f"%{term}%"),
                    Employee.last_name.ilike(f"%{term}%"),
                    Employee.employee_code.ilike(f"%{term}%"),
                    Employee.email.ilike(f"%{term}%"),
                )
            )
            .limit(limit)
        ).scalars().all()
        documents = db.execute(
            select(EmployeeDocument)
            .where(EmployeeDocument.file_name.ilike(f"%{term}%"))
            .limit(limit)
        ).scalars().all()

    pages = [
        {"type": "page", "id": "/employees", "label": "Employees"},
        {"type": "page", "id": "/payroll-runs", "label": "Payroll Runs"},
        {"type": "page", "id": "/expense-claims", "label": "Expense Claims"},
        {"type": "page", "id": "/loans", "label": "Loans"},
        {"type": "page", "id": "/settings/currencies", "label": "Currency Settings"},
    ]
    if term:
        pages = [p for p in pages if term.lower() in p["label"].lower()]

    reports = [
        {"type": "report", "id": r["code"], "label": r["title"]}
        for r in REPORTS
        if term and (term.lower() in r["title"].lower() or term.lower() in r["code"].lower())
    ]

    results = {
        "employees": [
            {
                "id": str(e.id),
                "label": f"{e.first_name} {e.last_name}".strip(),
                "sublabel": e.employee_code,
                "email": e.email,
            }
            for e in employees
        ],
        "documents": [
            {"id": str(d.id), "label": d.file_name, "sublabel": d.status} for d in documents
        ],
        "pages": pages,
        "reports": reports,
    }
    results["total"] = sum(len(v) for v in results.values() if isinstance(v, list))
    return {"data": results, "meta": {"query": term}, "errors": None}
