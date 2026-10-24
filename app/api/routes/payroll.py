"""Payroll run lifecycle, payslip release and salary revision endpoints."""
from __future__ import annotations

import csv
import io
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import FileResponse
from fpdf import FPDF
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import client_ip, get_current_user, require_permission, write_audit
from app.core.errors import BusinessRuleError
from app.crud.base import get_or_404, jsonable
from app.models.auth import User
from app.models.employee import Employee, EmployeeBankDetail
from app.models.payroll import (
    EmployeeSalary,
    PayGroup,
    PayrollRun,
    PayrollRunEmployee,
    Payslip,
    PayslipLine,
    SalaryComponent,
    SalaryRevision,
    SalaryStructureComponent,
    StatutorySetting,
)
from app.models.workflow import Approval, AuditLog

router = APIRouter(tags=["Payroll"])

PAYSLIP_DIR = Path("uploads/payslips")

MONEY = Decimal("0.01")


def _q(value: Decimal) -> Decimal:
    return Decimal(value).quantize(MONEY)


def _monthly_ctc(annual: Decimal) -> Decimal:
    return _q(Decimal(annual) / Decimal(12))


def _structure_breakdown(db: Session, salary: EmployeeSalary):
    """Very small illustrative component engine.

    Fixed components contribute their default value; percentage components are
    applied against monthly CTC when the formula is ``CTC * n%``.
    """
    monthly = _monthly_ctc(salary.ctc)
    lines: list[tuple[str, str, Decimal]] = []
    components = db.execute(
        select(SalaryStructureComponent, SalaryComponent)
        .join(SalaryComponent, SalaryComponent.id == SalaryStructureComponent.component_id)
        .where(SalaryStructureComponent.structure_id == salary.structure_id)
        .order_by(SalaryStructureComponent.sequence)
    ).all()
    if not components:
        return monthly, Decimal("0.00"), [(None, "Basic", "earning", monthly)]
    gross = Decimal("0.00")
    deductions = Decimal("0.00")
    for link, component in components:
        amount = Decimal(link.default_value or 0)
        if component.calculation_type == "percentage" and component.formula:
            try:
                pct = Decimal(str(component.formula).strip().rstrip("%")) / Decimal(100)
                amount = _q(monthly * pct)
            except Exception:  # noqa: BLE001
                amount = Decimal(link.default_value or 0)
        amount = _q(amount)
        if component.component_type == "deduction":
            deductions += amount
        else:
            gross += amount
        lines.append((component.id, component.name, component.component_type, amount))
    return gross, deductions, lines


class ProcessRunIn(BaseModel):
    include_new_joiners: bool = True


@router.post("/payroll-runs/{run_id}/process", summary="Populate run employees and compute pay")
def process_run(
    run_id: uuid.UUID,
    payload: ProcessRunIn | None = None,
    request: Request = None,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("payroll_run.edit")),
) -> dict:
    run = get_or_404(db, PayrollRun, run_id, "Payroll run")
    if run.status == "finalized":
        raise BusinessRuleError("Payroll run is finalized and locked")
    db.query(PayrollRunEmployee).filter(PayrollRunEmployee.payroll_run_id == run.id).delete()
    employees = db.execute(
        select(Employee).where(
            Employee.pay_group_id == run.pay_group_id, Employee.status.in_(["active", "probation"])
        )
    ).scalars().all()
    created = 0
    for employee in employees:
        salary = db.execute(
            select(EmployeeSalary)
            .where(EmployeeSalary.employee_id == employee.id, EmployeeSalary.status == "active")
            .order_by(EmployeeSalary.effective_from.desc())
        ).scalars().first()
        if salary is None:
            row = PayrollRunEmployee(
                payroll_run_id=run.id,
                employee_id=employee.id,
                gross=Decimal(0),
                deductions=Decimal(0),
                net_pay=Decimal(0),
                status="error",
                error_message="No active salary record",
            )
            db.add(row)
            created += 1
            continue
        gross, deductions, lines = _structure_breakdown(db, salary)
        net = gross - deductions
        row = PayrollRunEmployee(
            payroll_run_id=run.id,
            employee_id=employee.id,
            gross=_q(gross),
            deductions=_q(deductions),
            net_pay=_q(net),
            status="processed",
        )
        db.add(row)
        db.flush()
        created += 1
    if run.status == "draft":
        run.status = "preview"
    db.commit()
    return {"data": {"processed": created}, "meta": None, "errors": None}


@router.post("/payroll-runs/{run_id}/validate", summary="Pre-run statutory validation")
def validate_run(
    run_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("payroll_run.view")),
) -> dict:
    run = get_or_404(db, PayrollRun, run_id, "Payroll run")
    settings = db.execute(
        select(StatutorySetting).where(StatutorySetting.legal_entity_id == run.legal_entity_id)
    ).scalar_one_or_none()
    rows = db.execute(
        select(PayrollRunEmployee).where(PayrollRunEmployee.payroll_run_id == run.id)
    ).scalars().all()
    errors = [r for r in rows if r.status == "error"]
    checks = []
    enabled = {
        "PF": settings.pf_enabled if settings else True,
        "ESI": settings.esi_enabled if settings else True,
        "PT": settings.pt_enabled if settings else True,
        "LWF": settings.lwf_enabled if settings else True,
        "TDS": settings.tds_enabled if settings else True,
    }
    for name, on in enabled.items():
        if not on:
            checks.append({"name": name, "status": "warn", "message": f"{name} disabled"})
        elif settings is None:
            checks.append({"name": name, "status": "warn", "message": "No statutory settings configured"})
        else:
            checks.append({"name": name, "status": "pass", "message": "OK"})
    if not rows:
        checks.append({"name": "Employees", "status": "fail", "message": "No employees processed"})
    if errors:
        checks.append(
            {"name": "Data", "status": "warn", "message": f"{len(errors)} employees with errors"}
        )
    overall = "fail" if any(c["status"] == "fail" for c in checks) else (
        "warn" if any(c["status"] == "warn" for c in checks) else "pass"
    )
    return {"data": {"overall": overall, "checks": checks}, "meta": None, "errors": None}


@router.post("/payroll-runs/{run_id}/approve", summary="Approve a payroll run")
def approve_run(
    run_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("payroll_run.approve")),
) -> dict:
    run = get_or_404(db, PayrollRun, run_id, "Payroll run")
    if run.status == "finalized":
        raise BusinessRuleError("Payroll run already finalized")
    run.status = "approved"
    run.approved_by = user.id
    write_audit(
        db,
        user=user,
        entity_type="payroll_runs",
        entity_id=run.id,
        action="update",
        new_values={"status": "approved"},
        ip_address=client_ip(request),
        org_id=run.org_id,
    )
    db.commit()
    return {"data": {"status": run.status}, "meta": None, "errors": None}


@router.post("/payroll-runs/{run_id}/finalize", summary="Finalize a run and generate payslips")
def finalize_run(
    run_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("payroll_run.approve")),
) -> dict:
    run = get_or_404(db, PayrollRun, run_id, "Payroll run")
    if run.status == "finalized":
        raise BusinessRuleError("Payroll run already finalized")
    rows = db.execute(
        select(PayrollRunEmployee).where(PayrollRunEmployee.payroll_run_id == run.id)
    ).scalars().all()
    if not rows:
        raise BusinessRuleError("Nothing to finalize: no employees in this run")
    currency = (
        db.execute(select(PayGroup).where(PayGroup.id == run.pay_group_id)).scalar_one().currency
    )
    created_payslips = 0
    for row in rows:
        if row.status == "error":
            continue
        payslip = Payslip(
            payroll_run_employee_id=row.id,
            employee_id=row.employee_id,
            period_start=run.period_start,
            period_end=run.period_end,
            gross=row.gross,
            total_deductions=row.deductions,
            net_pay=row.net_pay,
            currency=currency,
            status="draft",
        )
        db.add(payslip)
        db.flush()
        db.add(
            PayslipLine(
                payslip_id=payslip.id,
                component_name="Gross Earnings",
                type="earning",
                amount=row.gross,
                currency=currency,
            )
        )
        db.add(
            PayslipLine(
                payslip_id=payslip.id,
                component_name="Total Deductions",
                type="deduction",
                amount=row.deductions,
                currency=currency,
            )
        )
        created_payslips += 1
    run.status = "finalized"
    run.finalized_at = datetime.now(timezone.utc)
    write_audit(
        db,
        user=user,
        entity_type="payroll_runs",
        entity_id=run.id,
        action="update",
        new_values={"status": "finalized", "payslips": created_payslips},
        ip_address=client_ip(request),
        org_id=run.org_id,
    )
    db.commit()
    return {"data": {"status": run.status, "payslips": created_payslips}, "meta": None, "errors": None}


@router.post("/payroll-runs/{run_id}/rollback", summary="Rollback a finalized run to draft")
def rollback_run(
    run_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("payroll_run.approve")),
) -> dict:
    run = get_or_404(db, PayrollRun, run_id, "Payroll run")
    db.query(Payslip).filter(
        Payslip.payroll_run_employee_id.in_(
            select(PayrollRunEmployee.id).where(PayrollRunEmployee.payroll_run_id == run.id)
        )
    ).delete(synchronize_session=False)
    run.status = "draft"
    run.finalized_at = None
    write_audit(
        db,
        user=user,
        entity_type="payroll_runs",
        entity_id=run.id,
        action="update",
        new_values={"status": "draft"},
        ip_address=client_ip(request),
        org_id=run.org_id,
    )
    db.commit()
    return {"data": {"status": run.status}, "meta": None, "errors": None}


@router.get("/payroll-runs/{run_id}/summary", summary="Payroll run summary cards")
def run_summary(
    run_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("payroll_run.view")),
) -> dict:
    run = get_or_404(db, PayrollRun, run_id, "Payroll run")
    rows = db.execute(
        select(PayrollRunEmployee).where(PayrollRunEmployee.payroll_run_id == run.id)
    ).scalars().all()
    gross = sum((r.gross for r in rows), Decimal(0))
    deductions = sum((r.deductions for r in rows), Decimal(0))
    net = sum((r.net_pay for r in rows), Decimal(0))
    return {
        "data": {
            "run_id": str(run.id),
            "status": run.status,
            "headcount": len(rows),
            "gross": str(_q(gross)),
            "deductions": str(_q(deductions)),
            "net": str(_q(net)),
            "employer_cost": str(_q(gross)),
        },
        "meta": None,
        "errors": None,
    }


@router.get("/payroll-runs/{run_id}/employees", summary="Employees in a payroll run")
def run_employees(
    run_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("payroll_run.view")),
) -> dict:
    get_or_404(db, PayrollRun, run_id, "Payroll run")
    rows = db.execute(
        select(PayrollRunEmployee).where(PayrollRunEmployee.payroll_run_id == run_id)
    ).scalars().all()
    return {
        "data": [
            {c.name: jsonable(getattr(r, c.name)) for c in PayrollRunEmployee.__table__.columns}
            for r in rows
        ],
        "meta": None,
        "errors": None,
    }


@router.get("/payroll-runs/{run_id}/bank-file", summary="Bank disbursement file (CSV)")
def bank_file(
    run_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("payroll_run.view")),
) -> Response:
    run = get_or_404(db, PayrollRun, run_id, "Payroll run")
    rows = db.execute(
        select(PayrollRunEmployee, Employee, EmployeeBankDetail)
        .join(Employee, Employee.id == PayrollRunEmployee.employee_id)
        .outerjoin(EmployeeBankDetail, EmployeeBankDetail.employee_id == Employee.id)
        .where(PayrollRunEmployee.payroll_run_id == run.id)
    ).all()
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        ["employee_code", "employee_name", "bank_name", "account_number", "ifsc", "amount"]
    )
    for run_emp, employee, bank in rows:
        writer.writerow(
            [
                employee.employee_code,
                f"{employee.first_name} {employee.last_name}".strip(),
                getattr(bank, "bank_name", "") or "",
                getattr(bank, "account_number", "") or "",
                getattr(bank, "ifsc_code", "") or "",
                str(run_emp.net_pay),
            ]
        )
    return Response(
        content=buffer.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="payroll_{run_id}_bank.csv"'},
    )


class PayslipActionIn(BaseModel):
    reason: str | None = None
    channels: list[str] = ["app", "email", "sms"]


@router.post("/payslips/{payslip_id}/release", summary="Release a payslip")
def release_payslip(
    payslip_id: uuid.UUID,
    payload: PayslipActionIn | None = None,
    request: Request = None,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("payslip.approve")),
) -> dict:
    payslip = get_or_404(db, Payslip, payslip_id, "Payslip")
    payslip.status = "released"
    payslip.released_at = datetime.now(timezone.utc)
    write_audit(
        db,
        user=user,
        entity_type="payslips",
        entity_id=payslip.id,
        action="update",
        new_values={"status": "released", "channels": (payload.channels if payload else None)},
        ip_address=client_ip(request),
    )
    db.commit()
    return {"data": {"status": payslip.status}, "meta": None, "errors": None}


@router.post("/payslips/{payslip_id}/hold", summary="Hold back a payslip")
def hold_payslip(
    payslip_id: uuid.UUID,
    payload: PayslipActionIn | None = None,
    request: Request = None,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("payslip.approve")),
) -> dict:
    payslip = get_or_404(db, Payslip, payslip_id, "Payslip")
    payslip.status = "held"
    write_audit(
        db,
        user=user,
        entity_type="payslips",
        entity_id=payslip.id,
        action="update",
        new_values={"status": "held", "reason": payload.reason if payload else None},
        ip_address=client_ip(request),
    )
    db.commit()
    return {"data": {"status": payslip.status}, "meta": None, "errors": None}


class SalaryRevisionIn(BaseModel):
    new_ctc: Decimal
    effective_date: date
    reason: str | None = None
    structure_id: uuid.UUID | None = None


@router.post("/employees/{employee_id}/salary-revisions", status_code=201,
             summary="Create a salary revision (creates a pending approval record)")
def create_salary_revision(
    employee_id: uuid.UUID,
    payload: SalaryRevisionIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("salary_revision.create")),
) -> dict:
    employee = get_or_404(db, Employee, employee_id, "Employee")
    current = db.execute(
        select(EmployeeSalary)
        .where(EmployeeSalary.employee_id == employee.id, EmployeeSalary.status == "active")
        .order_by(EmployeeSalary.effective_from.desc())
    ).scalars().first()
    revision = SalaryRevision(
        employee_id=employee.id,
        old_ctc=current.ctc if current else None,
        new_ctc=payload.new_ctc,
        effective_date=payload.effective_date,
        reason=payload.reason,
        status="pending",
    )
    db.add(revision)
    write_audit(
        db,
        user=user,
        entity_type="salary_revisions",
        entity_id=employee.id,
        action="create",
        new_values=payload.model_dump(),
        ip_address=client_ip(request),
        org_id=employee.org_id,
    )
    db.commit()
    db.refresh(revision)
    return {
        "data": {
            "id": str(revision.id),
            "status": revision.status,
            "old_ctc": jsonable(revision.old_ctc),
            "new_ctc": jsonable(revision.new_ctc),
        },
        "meta": None,
        "errors": None,
    }


class RevisionDecisionIn(BaseModel):
    reason: str | None = None


@router.post("/salary-revisions/{revision_id}/approve", summary="Approve a salary revision")
def approve_salary_revision(
    revision_id: uuid.UUID,
    payload: RevisionDecisionIn | None = None,
    request: Request = None,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("salary_revision.approve")),
) -> dict:
    revision = get_or_404(db, SalaryRevision, revision_id, "Salary revision")
    if revision.status != "pending":
        raise BusinessRuleError("Only pending revisions can be approved")
    current = db.execute(
        select(EmployeeSalary)
        .where(
            EmployeeSalary.employee_id == revision.employee_id,
            EmployeeSalary.status == "active",
        )
        .order_by(EmployeeSalary.effective_from.desc())
    ).scalars().first()
    structure_id = current.structure_id if current else None
    currency = current.currency if current else "INR"
    if current is not None:
        current.status = "superseded"
        current.effective_to = revision.effective_date
    db.add(
        EmployeeSalary(
            employee_id=revision.employee_id,
            structure_id=structure_id,
            ctc=revision.new_ctc,
            effective_from=revision.effective_date,
            currency=currency,
            status="active",
        )
    )
    revision.status = "approved"
    revision.approved_by = user.id
    write_audit(
        db,
        user=user,
        entity_type="salary_revisions",
        entity_id=revision.id,
        action="update",
        new_values={"status": "approved"},
        ip_address=client_ip(request),
    )
    db.commit()
    return {"data": {"status": revision.status}, "meta": None, "errors": None}


@router.post("/salary-revisions/{revision_id}/reject", summary="Reject a salary revision")
def reject_salary_revision(
    revision_id: uuid.UUID,
    payload: RevisionDecisionIn | None = None,
    request: Request = None,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("salary_revision.approve")),
) -> dict:
    revision = get_or_404(db, SalaryRevision, revision_id, "Salary revision")
    revision.status = "rejected"
    write_audit(
        db,
        user=user,
        entity_type="salary_revisions",
        entity_id=revision.id,
        action="update",
        new_values={"status": "rejected", "reason": payload.reason if payload else None},
        ip_address=client_ip(request),
    )
    db.commit()
    return {"data": {"status": revision.status}, "meta": None, "errors": None}


# ------------------------------------------------------- run analytics & tabs
def _statutory_breakdown(db: Session, run: PayrollRun):
    settings = db.execute(
        select(StatutorySetting).where(StatutorySetting.legal_entity_id == run.legal_entity_id)
    ).scalar_one_or_none()
    rows_in = db.execute(
        select(PayrollRunEmployee).where(PayrollRunEmployee.payroll_run_id == run.id)
    ).scalars().all()
    pf_on = settings.pf_enabled if settings else True
    esi_on = settings.esi_enabled if settings else True
    pt_on = settings.pt_enabled if settings else True
    lwf_on = settings.lwf_enabled if settings else True
    tds_on = settings.tds_enabled if settings else True

    pf, esi, pt, lwf, tds = [], [], [], [], []
    totals = {
        "pf_employee": Decimal("0"), "pf_employer": Decimal("0"), "pf_admin": Decimal("0"),
        "esi_employee": Decimal("0"), "esi_employer": Decimal("0"),
        "pt": Decimal("0"), "lwf_employee": Decimal("0"), "lwf_employer": Decimal("0"),
        "tds": Decimal("0"),
    }
    for row in rows_in:
        base = Decimal(row.gross or 0)
        if pf_on:
            pf_base = min(base, Decimal("15000"))
            emp = _q(pf_base * Decimal("0.12"))
            er = _q(pf_base * Decimal("0.12"))
            adm = _q(pf_base * Decimal("0.005"))
            pf.append({"employee_id": str(row.employee_id), "base": str(_q(pf_base)),
                       "employee": str(emp), "employer": str(er), "admin": str(adm)})
            totals["pf_employee"] += emp
            totals["pf_employer"] += er
            totals["pf_admin"] += adm
        if esi_on and base <= Decimal("21000"):
            emp = _q(base * Decimal("0.0075"))
            er = _q(base * Decimal("0.0325"))
            esi.append({"employee_id": str(row.employee_id), "gross": str(_q(base)),
                        "employee": str(emp), "employer": str(er)})
            totals["esi_employee"] += emp
            totals["esi_employer"] += er
        if pt_on:
            pt.append({"employee_id": str(row.employee_id), "amount": "200.00"})
            totals["pt"] += Decimal("200")
        if lwf_on:
            lwf.append({"employee_id": str(row.employee_id), "employee": "20.00", "employer": "40.00"})
            totals["lwf_employee"] += Decimal("20")
            totals["lwf_employer"] += Decimal("40")
        if tds_on:
            tds.append({"employee_id": str(row.employee_id), "amount": "0.00"})
    totals = {key: str(_q(value)) for key, value in totals.items()}
    return {
        "enabled": {"pf": pf_on, "esi": esi_on, "pt": pt_on, "lwf": lwf_on, "tds": tds_on},
        "pf": pf, "esi": esi, "pt": pt, "lwf": lwf, "tds": tds,
        "totals": totals,
    }


@router.get("/payroll-runs/{run_id}/statutory", summary="Statutory breakdown for a run")
def run_statutory(
    run_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("payroll_run.view")),
) -> dict:
    run = get_or_404(db, PayrollRun, run_id, "Payroll run")
    return {"data": _statutory_breakdown(db, run), "meta": None, "errors": None}


@router.get("/payroll-runs/{run_id}/challans", summary="Download a statutory challan CSV")
def run_challan(
    run_id: uuid.UUID,
    type: str = Query("pf", pattern="^(pf|esi|pt|tds|lwf)$"),
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("payroll_run.view")),
) -> Response:
    run = get_or_404(db, PayrollRun, run_id, "Payroll run")
    data = _statutory_breakdown(db, run)
    rows = data.get(type, [])
    buffer = io.StringIO()
    if rows:
        writer = csv.DictWriter(buffer, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    return Response(
        content=buffer.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{type}_{run_id}.csv"'},
    )


@router.get("/payroll-runs/{run_id}/variances", summary="Variances vs the previous run")
def run_variances(
    run_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("payroll_run.view")),
) -> dict:
    run = get_or_404(db, PayrollRun, run_id, "Payroll run")
    previous = db.execute(
        select(PayrollRun)
        .where(
            PayrollRun.pay_group_id == run.pay_group_id,
            PayrollRun.period_start < run.period_start,
            PayrollRun.status == "finalized",
        )
        .order_by(PayrollRun.period_start.desc())
    ).scalars().first()
    current_rows = {
        r.employee_id: r
        for r in db.execute(
            select(PayrollRunEmployee).where(PayrollRunEmployee.payroll_run_id == run.id)
        ).scalars().all()
    }
    previous_rows = {}
    if previous is not None:
        previous_rows = {
            r.employee_id: r
            for r in db.execute(
                select(PayrollRunEmployee).where(PayrollRunEmployee.payroll_run_id == previous.id)
            ).scalars().all()
        }
    data = []
    for employee_id, current in current_rows.items():
        prior = previous_rows.get(employee_id)
        prev_net = Decimal(prior.net_pay) if prior else Decimal("0")
        delta = Decimal(current.net_pay) - prev_net
        pct = float(delta / prev_net * 100) if prev_net else None
        data.append({
            "employee_id": str(employee_id),
            "current_gross": str(_q(current.gross)),
            "current_net": str(_q(current.net_pay)),
            "previous_net": str(_q(prev_net)),
            "delta": str(_q(delta)),
            "delta_pct": round(pct, 2) if pct is not None else None,
        })
    return {
        "data": data,
        "meta": {
            "previous_run_id": str(previous.id) if previous else None,
            "headcount": len(data),
        },
        "errors": None,
    }


@router.get("/payroll-runs/{run_id}/approvals", summary="Approvals for a payroll run")
def run_approvals(
    run_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("payroll_run.view")),
) -> dict:
    get_or_404(db, PayrollRun, run_id, "Payroll run")
    rows = db.execute(
        select(Approval).where(Approval.entity_type == "payroll_run", Approval.entity_id == run_id)
    ).scalars().all()
    return {
        "data": [
            {
                "id": str(a.id), "step_order": a.step_order, "status": a.status,
                "approver_id": jsonable(a.approver_id), "acted_at": jsonable(a.acted_at),
                "comments": a.comments,
            }
            for a in rows
        ],
        "meta": {"total": len(rows)},
        "errors": None,
    }


@router.get("/payroll-runs/{run_id}/audit", summary="Audit trail for a payroll run")
def run_audit(
    run_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("payroll_run.view")),
) -> dict:
    get_or_404(db, PayrollRun, run_id, "Payroll run")
    rows = db.execute(
        select(AuditLog)
        .where(AuditLog.entity_type == "payroll_runs", AuditLog.entity_id == run_id)
        .order_by(AuditLog.created_at.desc())
    ).scalars().all()
    return {
        "data": [
            {
                "id": str(a.id), "action": a.action, "user_id": jsonable(a.user_id),
                "old_values": a.old_values, "new_values": a.new_values,
                "ip_address": a.ip_address, "created_at": jsonable(a.created_at),
            }
            for a in rows
        ],
        "meta": {"total": len(rows)},
        "errors": None,
    }


# ------------------------------------------------------------------ payslip PDF
def _render_payslip_pdf(payslip: Payslip, lines: list, employee: Employee | None) -> str:
    PAYSLIP_DIR.mkdir(parents=True, exist_ok=True)
    path = PAYSLIP_DIR / f"{payslip.id}.pdf"

    def text(value) -> str:
        return str(value).encode("latin-1", "replace").decode("latin-1")

    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 10, text("Payslip"))
    pdf.ln(14)
    pdf.set_font("Helvetica", size=10)
    name = f"{employee.first_name} {employee.last_name}".strip() if employee else payslip.employee_id
    pdf.cell(0, 6, text(f"Employee: {name}"))
    pdf.ln(6)
    pdf.cell(0, 6, text(f"Period: {payslip.period_start} to {payslip.period_end}"))
    pdf.ln(6)
    pdf.cell(0, 6, text(f"Currency: {payslip.currency}   Status: {payslip.status}"))
    pdf.ln(10)
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(120, 7, text("Component"))
    pdf.cell(40, 7, text("Type"))
    pdf.cell(30, 7, text("Amount"))
    pdf.ln(7)
    pdf.set_font("Helvetica", size=10)
    for line in lines:
        pdf.cell(120, 6, text(line.component_name))
        pdf.cell(40, 6, text(line.type))
        pdf.cell(30, 6, text(str(line.amount)))
        pdf.ln(6)
    pdf.ln(4)
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(0, 6, text(f"Gross: {payslip.gross}   Deductions: {payslip.total_deductions}   Net: {payslip.net_pay}"))
    pdf.ln(6)
    pdf.output(str(path))
    return str(path)


@router.get("/payslips/{payslip_id}/pdf", summary="Generate/download a payslip PDF")
def payslip_pdf(
    payslip_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("payslip.view")),
):
    payslip = get_or_404(db, Payslip, payslip_id, "Payslip")
    lines = db.execute(
        select(PayslipLine).where(PayslipLine.payslip_id == payslip.id)
    ).scalars().all()
    employee = db.get(Employee, payslip.employee_id)
    path = _render_payslip_pdf(payslip, lines, employee)
    payslip.pdf_path = path
    db.add(payslip)
    db.commit()
    return FileResponse(path, media_type="application/pdf", filename=f"payslip_{payslip.id}.pdf")


@router.post("/payslips/{payslip_id}/reprint", summary="Regenerate a payslip PDF")
def reprint_payslip(
    payslip_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("payslip.approve")),
) -> dict:
    payslip = get_or_404(db, Payslip, payslip_id, "Payslip")
    lines = db.execute(
        select(PayslipLine).where(PayslipLine.payslip_id == payslip.id)
    ).scalars().all()
    employee = db.get(Employee, payslip.employee_id)
    path = _render_payslip_pdf(payslip, lines, employee)
    payslip.pdf_path = path
    write_audit(
        db, user=user, entity_type="payslips", entity_id=payslip.id,
        action="update", new_values={"reprint": True, "pdf_path": path},
        ip_address=client_ip(request),
    )
    db.commit()
    return {"data": {"payslip_id": str(payslip.id), "pdf_path": path}, "meta": None, "errors": None}


class BulkPayslipActionIn(BaseModel):
    ids: list[uuid.UUID]
    action: str = "release"
    reason: str | None = None


@router.post("/payslips/bulk-release", summary="Bulk release or hold payslips")
def bulk_payslip_release(
    payload: BulkPayslipActionIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("payslip.approve")),
) -> dict:
    if payload.action not in ("release", "hold"):
        raise BusinessRuleError("action must be 'release' or 'hold'")
    updated = 0
    now = datetime.now(timezone.utc)
    for payslip_id in payload.ids:
        payslip = db.get(Payslip, payslip_id)
        if payslip is None:
            continue
        if payload.action == "release":
            payslip.status = "released"
            payslip.released_at = now
        else:
            payslip.status = "held"
        db.add(payslip)
        updated += 1
    write_audit(
        db, user=user, entity_type="payslips", entity_id=payload.ids[0] if payload.ids else user.id,
        action="bulk_" + payload.action, new_values={"count": updated},
        ip_address=client_ip(request),
    )
    db.commit()
    return {"data": {"action": payload.action, "updated": updated}, "meta": None, "errors": None}
