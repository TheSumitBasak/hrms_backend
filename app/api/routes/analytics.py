"""Dashboard widgets, reports catalogue and report runners."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
import csv
import io
import json
import uuid

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_current_user, require_permission
from app.crud.base import jsonable
from app.models.auth import User
from app.models.employee import Employee
from app.models.expense import ExpenseClaim
from app.models.leave import LeaveRequest
from app.models.loan import Loan
from app.models.payroll import PayrollRun, PayrollRunEmployee, Payslip
from app.models.organization import Department
from app.models.user_pref import ReportSchedule
from app.models.workflow import Approval, HelpdeskTicket

router = APIRouter(tags=["Insights"])


def _month_start() -> date:
    today = date.today()
    return today.replace(day=1)


@router.get("/dashboard", summary="Role-aware dashboard widgets")
def dashboard(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    headcount_rows = db.execute(
        select(Employee.status, func.count()).group_by(Employee.status)
    ).all()
    headcount = {status: int(count) for status, count in headcount_rows}
    month_start = _month_start()
    joiners_mtd = int(
        db.execute(
            select(func.count())
            .select_from(Employee)
            .where(Employee.date_of_joining >= month_start)
        ).scalar_one()
    )
    exits_mtd = int(
        db.execute(
            select(func.count())
            .select_from(Employee)
            .where(Employee.date_of_exit >= month_start)
        ).scalar_one()
    )
    payroll_cost = db.execute(
        select(func.coalesce(func.sum(PayrollRunEmployee.net_pay), 0))
        .select_from(PayrollRunEmployee)
        .join(PayrollRun, PayrollRun.id == PayrollRunEmployee.payroll_run_id)
        .where(PayrollRun.period_start >= month_start)
    ).scalar_one()
    pending_approvals = int(
        db.execute(
            select(func.count())
            .select_from(Approval)
            .where(
                Approval.status == "pending",
                (Approval.approver_id == user.id) | (Approval.approver_id.is_(None)),
            )
        ).scalar_one()
    )
    open_tickets = int(
        db.execute(
            select(func.count())
            .select_from(HelpdeskTicket)
            .where(HelpdeskTicket.status != "closed")
        ).scalar_one()
    )
    pending_leave = int(
        db.execute(
            select(func.count()).select_from(LeaveRequest).where(LeaveRequest.status == "pending")
        ).scalar_one()
    )
    pending_expense = int(
        db.execute(
            select(func.count())
            .select_from(ExpenseClaim)
            .where(ExpenseClaim.status == "submitted")
        ).scalar_one()
    )
    active_loans = int(
        db.execute(
            select(func.count()).select_from(Loan).where(Loan.status == "active")
        ).scalar_one()
    )
    my_payslips = []
    if user.employee_id:
        rows = db.execute(
            select(Payslip)
            .where(Payslip.employee_id == user.employee_id)
            .order_by(Payslip.period_start.desc())
            .limit(3)
        ).scalars().all()
        my_payslips = [
            {
                "id": str(p.id),
                "period_start": jsonable(p.period_start),
                "period_end": jsonable(p.period_end),
                "net_pay": jsonable(p.net_pay),
                "currency": p.currency,
                "status": p.status,
            }
            for p in rows
        ]
    is_sqlite = db.bind.dialect.name == "sqlite"
    month_expr = (
        func.strftime("%Y-%m", Employee.date_of_exit)
        if is_sqlite
        else func.date_format(Employee.date_of_exit, "%Y-%m")
    )
    attrition_rows = db.execute(
        select(month_expr, func.count())
        .where(Employee.date_of_exit.is_not(None))
        .group_by(month_expr)
        .order_by(month_expr)
    ).all()
    attrition_trend = [{"month": month, "exits": int(count)} for month, count in attrition_rows]

    payroll_by_department = [
        {"department_id": jsonable(dept_id), "department": name, "net": jsonable(net)}
        for dept_id, name, net in db.execute(
            select(
                Employee.department_id,
                Department.name,
                func.coalesce(func.sum(PayrollRunEmployee.net_pay), 0),
            )
            .select_from(PayrollRunEmployee)
            .join(Employee, Employee.id == PayrollRunEmployee.employee_id)
            .outerjoin(Department, Department.id == Employee.department_id)
            .join(PayrollRun, PayrollRun.id == PayrollRunEmployee.payroll_run_id)
            .where(PayrollRun.period_start >= month_start)
            .group_by(Employee.department_id, Department.name)
        ).all()
    ]

    approvals = [
        {
            "id": str(a.id),
            "entity_type": a.entity_type,
            "entity_id": str(a.entity_id),
            "step_order": a.step_order,
            "status": a.status,
        }
        for a in db.execute(
            select(Approval)
            .where(Approval.status == "pending")
            .order_by(Approval.step_order)
            .limit(20)
        ).scalars().all()
    ]

    events = []
    for employee in db.execute(select(Employee).where(Employee.status.in_(["active", "probation"]))).scalars().all():
        if employee.date_of_birth is not None:
            events.append({
                "employee_id": str(employee.id),
                "employee_name": f"{employee.first_name} {employee.last_name}".strip(),
                "type": "birthday",
                "date": jsonable(employee.date_of_birth),
            })
        events.append({
            "employee_id": str(employee.id),
            "employee_name": f"{employee.first_name} {employee.last_name}".strip(),
            "type": "work_anniversary",
            "date": jsonable(employee.date_of_joining),
        })

    return {
        "data": {
            "headcount": {"total": sum(headcount.values()), "by_status": headcount},
            "joiners_mtd": joiners_mtd,
            "exits_mtd": exits_mtd,
            "payroll_cost_mtd": jsonable(Decimal(payroll_cost or 0).quantize(Decimal("0.01"))),
            "pending_approvals": pending_approvals,
            "pending_leave": pending_leave,
            "pending_expense": pending_expense,
            "active_loans": active_loans,
            "open_tickets": open_tickets,
            "my_payslips": my_payslips,
            "attrition_trend": attrition_trend,
            "payroll_by_department": payroll_by_department,
            "approvals": approvals,
            "events": events,
        },
        "meta": None,
        "errors": None,
    }


REPORTS = [
    {"code": "headcount", "title": "Headcount by department & status", "category": "HR"},
    {"code": "attrition", "title": "Attrition trend (12 months)", "category": "HR"},
    {"code": "payroll-register", "title": "Payroll register", "category": "Payroll"},
    {"code": "salary-summary", "title": "Monthly salary summary", "category": "Payroll"},
    {"code": "loan-outstanding", "title": "Loan outstanding balances", "category": "Finance"},
    {"code": "expense-summary", "title": "Expense spend by category", "category": "Finance"},
    {"code": "statutory-pf", "title": "PF contribution report", "category": "Compliance"},
    {"code": "statutory-esi", "title": "ESI contribution report", "category": "Compliance"},
]


@router.get("/reports", summary="Report catalogue")
def list_reports(_: User = Depends(get_current_user)) -> dict:
    return {"data": REPORTS, "meta": {"total": len(REPORTS)}, "errors": None}


class ReportScheduleIn(BaseModel):
    report_code: str
    frequency: str = "monthly"
    format: str = "csv"
    recipients: list[str] = []
    next_run_at: datetime | None = None
    is_active: bool = True


def _resolve_org(db: Session):
    from app.models.organization import Organization

    return db.execute(select(Organization).order_by(Organization.created_at)).scalars().first()


def _next_run(frequency: str) -> datetime:
    deltas = {"daily": 1, "weekly": 7, "monthly": 30, "quarterly": 90}
    return datetime.now(timezone.utc) + timedelta(days=deltas.get(frequency, 30))


@router.get("/reports/schedules", summary="List report schedules")
def list_schedules(
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("report.view")),
) -> dict:
    rows = db.execute(select(ReportSchedule).order_by(ReportSchedule.created_at.desc())).scalars().all()
    return {
        "data": [
            {
                "id": str(r.id), "report_code": r.report_code, "frequency": r.frequency,
                "format": r.format, "recipients": r.recipients or [],
                "next_run_at": jsonable(r.next_run_at), "is_active": r.is_active,
            }
            for r in rows
        ],
        "meta": {"total": len(rows)},
        "errors": None,
    }


@router.post("/reports/schedules", status_code=201, summary="Create a report schedule")
def create_schedule(
    payload: ReportScheduleIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("report.create")),
) -> dict:
    org = _resolve_org(db)
    if org is None:
        from app.core.errors import NotFoundError

        raise NotFoundError("Organization")
    schedule = ReportSchedule(
        org_id=org.id,
        report_code=payload.report_code,
        frequency=payload.frequency,
        format=payload.format,
        recipients=payload.recipients,
        next_run_at=payload.next_run_at or _next_run(payload.frequency),
        is_active=payload.is_active,
        created_by=user.id,
    )
    db.add(schedule)
    db.commit()
    db.refresh(schedule)
    return {"data": {"id": str(schedule.id), "next_run_at": jsonable(schedule.next_run_at)}, "meta": None, "errors": None}


@router.delete("/reports/schedules/{schedule_id}", summary="Delete a report schedule")
def delete_schedule(
    schedule_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("report.delete")),
) -> dict:
    schedule = db.get(ReportSchedule, schedule_id)
    if schedule is not None:
        db.delete(schedule)
        db.commit()
    return {"data": {"deleted": True}, "meta": None, "errors": None}


@router.get("/reports/{code}", summary="Run a report")
def run_report(
    code: str,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> dict:
    from app.core.errors import NotFoundError

    if code not in {report["code"] for report in REPORTS}:
        raise NotFoundError("Report", code)

    if code == "headcount":
        rows = db.execute(
            select(Employee.department_id, Employee.status, func.count())
            .group_by(Employee.department_id, Employee.status)
        ).all()
        data = [
            {"department_id": jsonable(dept), "status": status, "count": int(count)}
            for dept, status, count in rows
        ]
    elif code == "attrition":
        rows = db.execute(
            select(
                func.strftime("%Y-%m", Employee.date_of_exit) if db.bind.dialect.name == "sqlite"
                else func.to_char(Employee.date_of_exit, "YYYY-MM"),
                func.count(),
            )
            .where(Employee.date_of_exit.is_not(None))
            .group_by(
                func.strftime("%Y-%m", Employee.date_of_exit) if db.bind.dialect.name == "sqlite"
                else func.to_char(Employee.date_of_exit, "YYYY-MM")
            )
        ).all()
        data = [{"month": month, "exits": int(count)} for month, count in rows]
    elif code == "payroll-register":
        rows = db.execute(select(PayrollRunEmployee)).scalars().all()
        data = [
            {
                "run_id": str(r.payroll_run_id),
                "employee_id": str(r.employee_id),
                "gross": jsonable(r.gross),
                "deductions": jsonable(r.deductions),
                "net": jsonable(r.net_pay),
                "status": r.status,
            }
            for r in rows
        ]
    elif code == "salary-summary":
        rows = db.execute(
            select(PayrollRun.period_start, func.sum(PayrollRunEmployee.gross), func.sum(PayrollRunEmployee.net_pay))
            .join(PayrollRunEmployee, PayrollRunEmployee.payroll_run_id == PayrollRun.id)
            .group_by(PayrollRun.period_start)
        ).all()
        data = [
            {"period_start": jsonable(period), "gross": jsonable(gross), "net": jsonable(net)}
            for period, gross, net in rows
        ]
    elif code == "loan-outstanding":
        rows = db.execute(select(Loan).where(Loan.status == "active")).scalars().all()
        data = [
            {
                "loan_id": str(loan.id),
                "employee_id": str(loan.employee_id),
                "outstanding": jsonable(loan.outstanding),
                "emi": jsonable(loan.emi_amount),
            }
            for loan in rows
        ]
    elif code == "expense-summary":
        from app.models.expense import ExpenseClaimLine

        rows = db.execute(
            select(ExpenseClaimLine.category, func.sum(ExpenseClaimLine.amount))
            .group_by(ExpenseClaimLine.category)
        ).all()
        data = [{"category": category, "amount": jsonable(amount)} for category, amount in rows]
    else:
        rows = db.execute(
            select(PayrollRun).order_by(PayrollRun.period_start.desc()).limit(12)
        ).scalars().all()
        data = [
            {
                "run_id": str(run.id),
                "period_start": jsonable(run.period_start),
                "period_end": jsonable(run.period_end),
                "status": run.status,
            }
            for run in rows
        ]
    return {"data": data, "meta": {"report": code, "rows": len(data)}, "errors": None}


@router.get("/reports/{code}/export", summary="Export a report as CSV or JSON")
def export_report(
    code: str,
    format: str = Query("csv", pattern="^(csv|json)$"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Response:
    payload = run_report(code=code, db=db, _=user)
    rows = payload["data"]
    if format == "json":
        return Response(
            content=json.dumps(rows, default=str),
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="report_{code}.json"'},
        )
    buffer = io.StringIO()
    if rows:
        writer = csv.DictWriter(buffer, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    return Response(
        content=buffer.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="report_{code}.csv"'},
    )
