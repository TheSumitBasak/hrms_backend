"""Employee-centric nested endpoints (profile, job changes, documents, timeline)."""
from __future__ import annotations

import shutil
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import client_ip, get_current_user, require_permission, write_audit
from app.core.errors import APIError
from app.crud.base import get_or_404, jsonable
from app.models.employee import (
    Employee,
    EmployeeBankDetail,
    EmployeeDocument,
    EmployeeJobHistory,
    EmployeePersonalDetail,
    EmployeeStatutoryDetail,
)
from app.models.lifecycle import EmployeeOnboarding
from app.models.payroll import EmployeeSalary, Payslip
from app.models.leave import LeaveBalance
from app.models.auth import User

router = APIRouter(prefix="/employees", tags=["People"])

AVATAR_DIR = Path("uploads/avatars")


class PersonalDetailsIn(BaseModel):
    address_line1: str | None = None
    address_line2: str | None = None
    city: str | None = None
    state: str | None = None
    country: str | None = None
    postal_code: str | None = None
    emergency_contact_name: str | None = None
    emergency_contact_phone: str | None = None
    blood_group: str | None = None
    custom_fields: dict | None = None


class BankDetailsIn(BaseModel):
    account_name: str
    account_number: str
    bank_name: str
    ifsc_code: str | None = None
    branch: str | None = None
    account_type: str | None = None
    is_primary: bool = True


class StatutoryDetailsIn(BaseModel):
    pan: str | None = None
    aadhaar: str | None = None
    uan: str | None = None
    pf_number: str | None = None
    esic_number: str | None = None
    pt_state: str | None = None
    lwf_state: str | None = None
    tax_regime: str | None = None


class JobChangeIn(BaseModel):
    effective_date: date
    legal_entity_id: uuid.UUID | None = None
    department_id: uuid.UUID | None = None
    designation_id: uuid.UUID | None = None
    location_id: uuid.UUID | None = None
    cost_center_id: uuid.UUID | None = None
    reporting_manager_id: uuid.UUID | None = None
    change_reason: str | None = None
    apply_to_employee: bool = True


def _model_dict(obj, fields: tuple[str, ...]) -> dict:
    return {field: jsonable(getattr(obj, field)) for field in fields}


@router.get("/{employee_id}/profile", summary="Full employee profile bundle")
def employee_profile(
    employee_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("employee.view")),
) -> dict:
    employee = get_or_404(db, Employee, employee_id, "Employee")
    personal = db.get(EmployeePersonalDetail, employee.id)
    statutory = db.execute(
        select(EmployeeStatutoryDetail).where(EmployeeStatutoryDetail.employee_id == employee.id)
    ).scalar_one_or_none()
    banks = db.execute(
        select(EmployeeBankDetail).where(EmployeeBankDetail.employee_id == employee.id)
    ).scalars().all()
    history = db.execute(
        select(EmployeeJobHistory)
        .where(EmployeeJobHistory.employee_id == employee.id)
        .order_by(EmployeeJobHistory.effective_date.desc())
    ).scalars().all()
    return {
        "data": {
            "employee": _model_dict(employee, tuple(c.name for c in Employee.__table__.columns)),
            "personal_details": _model_dict(
                personal, tuple(c.name for c in EmployeePersonalDetail.__table__.columns)
            )
            if personal
            else None,
            "statutory_details": _model_dict(
                statutory, tuple(c.name for c in EmployeeStatutoryDetail.__table__.columns)
            )
            if statutory
            else None,
            "bank_details": [
                _model_dict(b, tuple(c.name for c in EmployeeBankDetail.__table__.columns))
                for b in banks
            ],
            "job_history": [
                _model_dict(h, tuple(c.name for c in EmployeeJobHistory.__table__.columns))
                for h in history
            ],
        },
        "meta": None,
        "errors": None,
    }


@router.put("/{employee_id}/personal-details", summary="Upsert employee personal details")
def upsert_personal_details(
    employee_id: uuid.UUID,
    payload: PersonalDetailsIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("employee.edit")),
) -> dict:
    employee = get_or_404(db, Employee, employee_id, "Employee")
    personal = db.get(EmployeePersonalDetail, employee.id)
    if personal is None:
        personal = EmployeePersonalDetail(employee_id=employee.id, **payload.model_dump())
        db.add(personal)
    else:
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(personal, key, value)
    write_audit(
        db,
        user=user,
        entity_type="employee_personal_details",
        entity_id=employee.id,
        action="update",
        new_values=payload.model_dump(exclude_unset=True),
        ip_address=client_ip(request),
        org_id=employee.org_id,
    )
    db.commit()
    return {
        "data": _model_dict(
            personal, tuple(c.name for c in EmployeePersonalDetail.__table__.columns)
        ),
        "meta": None,
        "errors": None,
    }


@router.post("/{employee_id}/bank-details", status_code=201, summary="Add employee bank account")
def add_bank_details(
    employee_id: uuid.UUID,
    payload: BankDetailsIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("employee.edit")),
) -> dict:
    employee = get_or_404(db, Employee, employee_id, "Employee")
    record = EmployeeBankDetail(employee_id=employee.id, **payload.model_dump())
    db.add(record)
    write_audit(
        db,
        user=user,
        entity_type="employee_bank_details",
        entity_id=employee.id,
        action="create",
        new_values=payload.model_dump(),
        ip_address=client_ip(request),
        org_id=employee.org_id,
    )
    db.commit()
    db.refresh(record)
    return {
        "data": _model_dict(record, tuple(c.name for c in EmployeeBankDetail.__table__.columns)),
        "meta": None,
        "errors": None,
    }


@router.put("/{employee_id}/statutory-details", summary="Upsert employee statutory details")
def upsert_statutory_details(
    employee_id: uuid.UUID,
    payload: StatutoryDetailsIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("employee.edit")),
) -> dict:
    employee = get_or_404(db, Employee, employee_id, "Employee")
    record = db.execute(
        select(EmployeeStatutoryDetail).where(EmployeeStatutoryDetail.employee_id == employee.id)
    ).scalar_one_or_none()
    if record is None:
        record = EmployeeStatutoryDetail(employee_id=employee.id, **payload.model_dump())
        db.add(record)
    else:
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(record, key, value)
    write_audit(
        db,
        user=user,
        entity_type="employee_statutory_details",
        entity_id=employee.id,
        action="update",
        new_values=payload.model_dump(exclude_unset=True),
        ip_address=client_ip(request),
        org_id=employee.org_id,
    )
    db.commit()
    return {
        "data": _model_dict(
            record, tuple(c.name for c in EmployeeStatutoryDetail.__table__.columns)
        ),
        "meta": None,
        "errors": None,
    }


@router.post("/{employee_id}/job-change", summary="Apply a job change and record history")
def job_change(
    employee_id: uuid.UUID,
    payload: JobChangeIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("employee.edit")),
) -> dict:
    employee = get_or_404(db, Employee, employee_id, "Employee")
    history = EmployeeJobHistory(
        employee_id=employee.id,
        effective_date=payload.effective_date,
        legal_entity_id=payload.legal_entity_id or employee.legal_entity_id,
        department_id=payload.department_id or employee.department_id,
        designation_id=payload.designation_id or employee.designation_id,
        location_id=payload.location_id or employee.location_id,
        cost_center_id=payload.cost_center_id or employee.cost_center_id,
        reporting_manager_id=payload.reporting_manager_id or employee.reporting_manager_id,
        change_reason=payload.change_reason,
    )
    db.add(history)
    if payload.apply_to_employee:
        for field in (
            "legal_entity_id",
            "department_id",
            "designation_id",
            "location_id",
            "cost_center_id",
            "reporting_manager_id",
        ):
            value = getattr(payload, field)
            if value is not None:
                setattr(employee, field, value)
    write_audit(
        db,
        user=user,
        entity_type="employee",
        entity_id=employee.id,
        action="update",
        new_values=payload.model_dump(),
        ip_address=client_ip(request),
        org_id=employee.org_id,
    )
    db.commit()
    return {"data": {"history_id": str(history.id)}, "meta": None, "errors": None}


@router.get("/{employee_id}/salaries", summary="Salary records for an employee")
def employee_salaries(
    employee_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("employee_salary.view")),
) -> dict:
    get_or_404(db, Employee, employee_id, "Employee")
    rows = db.execute(
        select(EmployeeSalary)
        .where(EmployeeSalary.employee_id == employee_id)
        .order_by(EmployeeSalary.effective_from.desc())
    ).scalars().all()
    return {
        "data": [_model_dict(r, tuple(c.name for c in EmployeeSalary.__table__.columns)) for r in rows],
        "meta": None,
        "errors": None,
    }


@router.get("/{employee_id}/payslips", summary="Payslips for an employee")
def employee_payslips(
    employee_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("payslip.view")),
) -> dict:
    get_or_404(db, Employee, employee_id, "Employee")
    rows = db.execute(
        select(Payslip).where(Payslip.employee_id == employee_id).order_by(Payslip.period_start.desc())
    ).scalars().all()
    return {
        "data": [_model_dict(r, tuple(c.name for c in Payslip.__table__.columns)) for r in rows],
        "meta": None,
        "errors": None,
    }


@router.get("/{employee_id}/documents", summary="Documents for an employee")
def employee_documents(
    employee_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("employee_document.view")),
) -> dict:
    get_or_404(db, Employee, employee_id, "Employee")
    rows = db.execute(
        select(EmployeeDocument).where(EmployeeDocument.employee_id == employee_id)
    ).scalars().all()
    return {
        "data": [_model_dict(r, tuple(c.name for c in EmployeeDocument.__table__.columns)) for r in rows],
        "meta": None,
        "errors": None,
    }


@router.get("/{employee_id}/leave-balances", summary="Leave balances for an employee")
def employee_leave_balances(
    employee_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("leave.view")),
) -> dict:
    get_or_404(db, Employee, employee_id, "Employee")
    rows = db.execute(
        select(LeaveBalance).where(LeaveBalance.employee_id == employee_id)
    ).scalars().all()
    return {
        "data": [_model_dict(r, tuple(c.name for c in LeaveBalance.__table__.columns)) for r in rows],
        "meta": None,
        "errors": None,
    }


@router.get("/directory/list", summary="Searchable employee directory")
def directory(
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("employee.view")),
) -> dict:
    rows = db.execute(
        select(Employee).where(Employee.status == "active").order_by(Employee.first_name)
    ).scalars().all()
    return {
        "data": [
            {
                "id": str(e.id),
                "employee_code": e.employee_code,
                "full_name": f"{e.first_name} {e.last_name}".strip(),
                "email": e.email,
                "department_id": jsonable(e.department_id),
                "designation_id": jsonable(e.designation_id),
                "location_id": jsonable(e.location_id),
                "reporting_manager_id": jsonable(e.reporting_manager_id),
            }
            for e in rows
        ],
        "meta": {"total": len(rows)},
        "errors": None,
    }


@router.get("/{employee_id}/onboarding", summary="Onboarding records for an employee")
def employee_onboarding(
    employee_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("onboarding.view")),
) -> dict:
    get_or_404(db, Employee, employee_id, "Employee")
    rows = db.execute(
        select(EmployeeOnboarding)
        .where(EmployeeOnboarding.employee_id == employee_id)
        .order_by(EmployeeOnboarding.started_at.desc())
    ).scalars().all()
    return {
        "data": [
            {c.name: jsonable(getattr(r, c.name)) for c in EmployeeOnboarding.__table__.columns}
            for r in rows
        ],
        "meta": {"total": len(rows)},
        "errors": None,
    }


@router.post("/{employee_id}/avatar", summary="Upload an employee avatar")
def upload_avatar(
    employee_id: uuid.UUID,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("employee.edit")),
) -> dict:
    employee = get_or_404(db, Employee, employee_id, "Employee")
    AVATAR_DIR.mkdir(parents=True, exist_ok=True)
    suffix = Path(file.filename or "").suffix or ".png"
    destination = AVATAR_DIR / f"{employee.id}{suffix}"
    with destination.open("wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
    if destination.stat().st_size > 5 * 1024 * 1024:
        destination.unlink(missing_ok=True)
        raise APIError(400, "Avatar exceeds the 5MB limit", "VALIDATION")
    employee.avatar_path = str(destination)
    db.add(employee)
    db.commit()
    return {"data": {"avatar_path": employee.avatar_path}, "meta": None, "errors": None}


@router.get("/{employee_id}/avatar", summary="Fetch an employee avatar")
def get_avatar(
    employee_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("employee.view")),
):
    employee = get_or_404(db, Employee, employee_id, "Employee")
    if not employee.avatar_path:
        raise APIError(404, "No avatar uploaded", "NOT_FOUND")
    path = Path(employee.avatar_path)
    if not path.exists():
        raise APIError(404, "Stored avatar not found", "NOT_FOUND")
    return FileResponse(path)

