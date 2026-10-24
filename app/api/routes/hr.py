"""HR operations: leave, attendance, onboarding, exit, assets and helpdesk."""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import client_ip, require_permission, write_audit
from app.core.errors import BusinessRuleError
from app.crud.base import get_or_404, jsonable
from app.models.auth import User
from app.models.attendance import AttendanceRecord
from app.models.employee import Employee
from app.models.leave import LeaveBalance, LeaveRequest, LeaveType
from app.models.lifecycle import (
    Asset,
    AssetAssignment,
    EmployeeOnboarding,
    ExitRequest,
    ExitTask,
)
from app.models.loan import Loan
from app.models.payroll import EmployeeSalary
from app.models.workflow import AuditLog, HelpdeskTicket

router = APIRouter(tags=["HR Operations"])

MONEY = Decimal("0.01")


def _q(value: Decimal) -> Decimal:
    return Decimal(value).quantize(MONEY)


class DecisionIn(BaseModel):
    reason: str | None = None
    comments: str | None = None


# ---------------------------------------------------------------------- Leave
@router.post("/leave-requests/{request_id}/approve", summary="Approve a leave request")
def approve_leave(
    request_id: uuid.UUID,
    payload: DecisionIn | None = None,
    request: Request = None,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("leave.approve")),
) -> dict:
    leave = get_or_404(db, LeaveRequest, request_id, "Leave request")
    if leave.status != "pending":
        raise BusinessRuleError("Only pending leave requests can be approved")
    leave.status = "approved"
    leave.approved_by = user.id
    balance = db.execute(
        select(LeaveBalance).where(
            LeaveBalance.employee_id == leave.employee_id,
            LeaveBalance.leave_type_id == leave.leave_type_id,
        )
    ).scalar_one_or_none()
    if balance is not None:
        balance.used = (Decimal(balance.used) + Decimal(leave.days)).quantize(Decimal("0.01"))
        balance.available = (Decimal(balance.balance) - Decimal(balance.used)).quantize(
            Decimal("0.01")
        )
    write_audit(
        db,
        user=user,
        entity_type="leave_requests",
        entity_id=leave.id,
        action="update",
        new_values={"status": "approved"},
        ip_address=client_ip(request),
    )
    db.commit()
    return {"data": {"status": leave.status}, "meta": None, "errors": None}


@router.post("/leave-requests/{request_id}/reject", summary="Reject a leave request")
def reject_leave(
    request_id: uuid.UUID,
    payload: DecisionIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("leave.approve")),
) -> dict:
    leave = get_or_404(db, LeaveRequest, request_id, "Leave request")
    leave.status = "rejected"
    write_audit(
        db,
        user=user,
        entity_type="leave_requests",
        entity_id=leave.id,
        action="update",
        new_values={"status": "rejected", "reason": payload.reason},
        ip_address=client_ip(request),
    )
    db.commit()
    return {"data": {"status": leave.status}, "meta": None, "errors": None}


@router.post("/leave-requests/{request_id}/cancel", summary="Cancel a leave request")
def cancel_leave(
    request_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("leave.edit")),
) -> dict:
    leave = get_or_404(db, LeaveRequest, request_id, "Leave request")
    was_approved = leave.status == "approved"
    leave.status = "cancelled"
    if was_approved:
        balance = db.execute(
            select(LeaveBalance).where(
                LeaveBalance.employee_id == leave.employee_id,
                LeaveBalance.leave_type_id == leave.leave_type_id,
            )
        ).scalar_one_or_none()
        if balance is not None:
            balance.used = max(
                Decimal("0"), (Decimal(balance.used) - Decimal(leave.days))
            ).quantize(Decimal("0.01"))
            balance.available = (Decimal(balance.balance) - Decimal(balance.used)).quantize(
                Decimal("0.01")
            )
    db.commit()
    return {"data": {"status": leave.status}, "meta": None, "errors": None}


# ----------------------------------------------------------------- Attendance
class RegulariseIn(BaseModel):
    clock_in: datetime | None = None
    clock_out: datetime | None = None
    status: str = "present"
    reason: str | None = None


@router.post("/attendance-records/{record_id}/regularise", summary="Regularise an attendance record")
def regularise_attendance(
    record_id: uuid.UUID,
    payload: RegulariseIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("attendance.edit")),
) -> dict:
    record = get_or_404(db, AttendanceRecord, record_id, "Attendance record")
    record.clock_in = payload.clock_in or record.clock_in
    record.clock_out = payload.clock_out or record.clock_out
    record.status = payload.status
    if record.clock_in and record.clock_out:
        delta = record.clock_out - record.clock_in
        record.worked_hours = Decimal(str(round(delta.total_seconds() / 3600, 2)))
    write_audit(
        db,
        user=user,
        entity_type="attendance_records",
        entity_id=record.id,
        action="update",
        new_values=payload.model_dump(mode="json"),
        ip_address=client_ip(request),
    )
    db.commit()
    return {
        "data": {
            "id": str(record.id),
            "status": record.status,
            "worked_hours": jsonable(record.worked_hours),
        },
        "meta": None,
        "errors": None,
    }


# ---------------------------------------------------------------- Onboarding
class StartOnboardingIn(BaseModel):
    template_id: uuid.UUID


@router.post("/employees/{employee_id}/onboarding", status_code=201, summary="Start onboarding")
def start_onboarding(
    employee_id: uuid.UUID,
    payload: StartOnboardingIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("onboarding.edit")),
) -> dict:
    employee = get_or_404(db, Employee, employee_id, "Employee")
    record = EmployeeOnboarding(
        employee_id=employee.id,
        template_id=payload.template_id,
        status="in_progress",
        started_at=datetime.now(timezone.utc),
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return {"data": {"id": str(record.id), "status": record.status}, "meta": None, "errors": None}


@router.post("/employee-onboarding/{onboarding_id}/complete", summary="Complete onboarding")
def complete_onboarding(
    onboarding_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("onboarding.edit")),
) -> dict:
    record = get_or_404(db, EmployeeOnboarding, onboarding_id, "Onboarding record")
    record.status = "completed"
    record.completed_at = datetime.now(timezone.utc)
    db.commit()
    return {"data": {"status": record.status}, "meta": None, "errors": None}


# ----------------------------------------------------------------------- Exit
@router.post("/exit-requests/{exit_id}/approve", summary="Approve exit request")
def approve_exit(
    exit_id: uuid.UUID,
    payload: DecisionIn | None = None,
    request: Request = None,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("exit.approve")),
) -> dict:
    exit_request = get_or_404(db, ExitRequest, exit_id, "Exit request")
    exit_request.status = "approved"
    exit_request.approved_by = user.id
    write_audit(
        db,
        user=user,
        entity_type="exit_requests",
        entity_id=exit_request.id,
        action="update",
        new_values={"status": "approved"},
        ip_address=client_ip(request),
    )
    db.commit()
    return {"data": {"status": exit_request.status}, "meta": None, "errors": None}


@router.post("/exit-requests/{exit_id}/complete", summary="Complete exit and offboard employee")
def complete_exit(
    exit_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("exit.edit")),
) -> dict:
    exit_request = get_or_404(db, ExitRequest, exit_id, "Exit request")
    if exit_request.status != "approved":
        raise BusinessRuleError("Exit request must be approved first")
    exit_request.status = "completed"
    employee = db.get(Employee, exit_request.employee_id)
    if employee is not None:
        employee.status = "exited"
        employee.date_of_exit = exit_request.last_working_date
    write_audit(
        db,
        user=user,
        entity_type="exit_requests",
        entity_id=exit_request.id,
        action="update",
        new_values={"status": "completed"},
        ip_address=client_ip(request),
    )
    db.commit()
    return {"data": {"status": exit_request.status}, "meta": None, "errors": None}


@router.post("/exit-tasks/{task_id}/complete", summary="Complete an exit checklist task")
def complete_exit_task(
    task_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("exit.edit")),
) -> dict:
    task = get_or_404(db, ExitTask, task_id, "Exit task")
    task.status = "completed"
    task.completed_at = datetime.now(timezone.utc)
    db.commit()
    return {"data": {"status": task.status}, "meta": None, "errors": None}


# --------------------------------------------------------------------- Assets
class AssignAssetIn(BaseModel):
    employee_id: uuid.UUID
    assigned_date: date
    condition: str | None = None


@router.post("/assets/{asset_id}/assign", status_code=201, summary="Assign an asset to an employee")
def assign_asset(
    asset_id: uuid.UUID,
    payload: AssignAssetIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("asset.edit")),
) -> dict:
    asset = get_or_404(db, Asset, asset_id, "Asset")
    if asset.status == "assigned":
        raise BusinessRuleError("Asset is already assigned")
    asset.status = "assigned"
    assignment = AssetAssignment(
        asset_id=asset.id,
        employee_id=payload.employee_id,
        assigned_date=payload.assigned_date,
        status="assigned",
    )
    db.add(assignment)
    db.commit()
    db.refresh(assignment)
    return {"data": {"assignment_id": str(assignment.id)}, "meta": None, "errors": None}


class ReturnAssetIn(BaseModel):
    returned_date: date
    condition_on_return: str | None = None


@router.post("/asset-assignments/{assignment_id}/return", summary="Return an assigned asset")
def return_asset(
    assignment_id: uuid.UUID,
    payload: ReturnAssetIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("asset.edit")),
) -> dict:
    assignment = get_or_404(db, AssetAssignment, assignment_id, "Asset assignment")
    assignment.status = "returned"
    assignment.returned_date = payload.returned_date
    assignment.condition_on_return = payload.condition_on_return
    asset = db.get(Asset, assignment.asset_id)
    if asset is not None:
        asset.status = "available"
    db.commit()
    return {"data": {"status": assignment.status}, "meta": None, "errors": None}


# ------------------------------------------------------------------- Helpdesk
class AssignTicketIn(BaseModel):
    assigned_to: uuid.UUID
    sla_due_at: datetime | None = None


@router.post("/helpdesk-tickets/{ticket_id}/assign", summary="Assign a helpdesk ticket")
def assign_ticket(
    ticket_id: uuid.UUID,
    payload: AssignTicketIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("helpdesk.edit")),
) -> dict:
    ticket = get_or_404(db, HelpdeskTicket, ticket_id, "Ticket")
    ticket.assigned_to = payload.assigned_to
    ticket.sla_due_at = payload.sla_due_at
    ticket.status = "in_progress"
    db.commit()
    return {"data": {"status": ticket.status}, "meta": None, "errors": None}


@router.post("/helpdesk-tickets/{ticket_id}/close", summary="Close a helpdesk ticket")
def close_ticket(
    ticket_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("helpdesk.edit")),
) -> dict:
    ticket = get_or_404(db, HelpdeskTicket, ticket_id, "Ticket")
    ticket.status = "closed"
    db.commit()
    return {"data": {"status": ticket.status}, "meta": None, "errors": None}


# ------------------------------------------------------------- leave overlap
@router.get("/leave-requests/team-overlap", summary="Detect overlapping leave among teammates")
def team_overlap(
    employee_id: uuid.UUID,
    start: date,
    end: date,
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("leave.view")),
) -> dict:
    employee = get_or_404(db, Employee, employee_id, "Employee")
    teammate_query = select(Employee).where(Employee.id != employee.id)
    if employee.reporting_manager_id:
        teammate_query = teammate_query.where(
            or_(
                Employee.reporting_manager_id == employee.reporting_manager_id,
                Employee.id == employee.reporting_manager_id,
            )
        )
    elif employee.department_id:
        teammate_query = teammate_query.where(Employee.department_id == employee.department_id)
    teammates = db.execute(teammate_query).scalars().all()
    teammate_ids = {t.id: t for t in teammates}
    overlaps = []
    if teammate_ids:
        rows = db.execute(
            select(LeaveRequest).where(
                LeaveRequest.employee_id.in_(list(teammate_ids)),
                LeaveRequest.status.in_(["pending", "approved"]),
                LeaveRequest.start_date <= end,
                LeaveRequest.end_date >= start,
            )
        ).scalars().all()
        for leave in rows:
            person = teammate_ids.get(leave.employee_id)
            overlaps.append({
                "employee_id": str(leave.employee_id),
                "employee_name": f"{person.first_name} {person.last_name}".strip() if person else None,
                "leave_request_id": str(leave.id),
                "start_date": jsonable(leave.start_date),
                "end_date": jsonable(leave.end_date),
                "status": leave.status,
            })
    return {
        "data": {"has_overlap": bool(overlaps), "overlaps": overlaps},
        "meta": {"teammates_checked": len(teammates)},
        "errors": None,
    }


# ------------------------------------------------------------ attendance extras
@router.get("/attendance-records/summary", summary="Attendance counts over a period")
def attendance_summary(
    from_date: date = Query(..., alias="from"),
    to_date: date = Query(..., alias="to"),
    employee_id: uuid.UUID | None = Query(None),
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("attendance.view")),
) -> dict:
    stmt = (
        select(AttendanceRecord.status, func.count())
        .where(AttendanceRecord.date >= from_date, AttendanceRecord.date <= to_date)
        .group_by(AttendanceRecord.status)
    )
    if employee_id:
        stmt = stmt.where(AttendanceRecord.employee_id == employee_id)
    rows = db.execute(stmt).all()
    counts = {status: int(count) for status, count in rows}
    return {
        "data": {"from": from_date.isoformat(), "to": to_date.isoformat(), "counts": counts,
                 "total": sum(counts.values())},
        "meta": None,
        "errors": None,
    }


@router.get("/attendance-records/{record_id}/timeline", summary="Attendance record timeline")
def attendance_timeline(
    record_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("attendance.view")),
) -> dict:
    record = get_or_404(db, AttendanceRecord, record_id, "Attendance record")
    history = db.execute(
        select(AuditLog)
        .where(AuditLog.entity_type == "attendance_records", AuditLog.entity_id == record_id)
        .order_by(AuditLog.created_at.desc())
    ).scalars().all()
    return {
        "data": {
            "record": {c.name: jsonable(getattr(record, c.name)) for c in AttendanceRecord.__table__.columns},
            "events": [
                {"action": h.action, "old_values": h.old_values, "new_values": h.new_values,
                 "at": jsonable(h.created_at), "ip_address": h.ip_address}
                for h in history
            ],
        },
        "meta": None,
        "errors": None,
    }


class MarkManualIn(BaseModel):
    clock_in: datetime | None = None
    clock_out: datetime | None = None
    status: str = "present"
    reason: str | None = None


@router.post("/attendance-records/{record_id}/mark-manual", summary="Mark an attendance record as manual")
def mark_manual_attendance(
    record_id: uuid.UUID,
    payload: MarkManualIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("attendance.edit")),
) -> dict:
    record = get_or_404(db, AttendanceRecord, record_id, "Attendance record")
    record.clock_in = payload.clock_in or record.clock_in
    record.clock_out = payload.clock_out or record.clock_out
    record.status = payload.status
    record.source = "manual"
    if record.clock_in and record.clock_out:
        delta = record.clock_out - record.clock_in
        record.worked_hours = Decimal(str(round(delta.total_seconds() / 3600, 2)))
    write_audit(
        db, user=user, entity_type="attendance_records", entity_id=record.id,
        action="mark_manual", new_values=payload.model_dump(mode="json"),
        ip_address=client_ip(request),
    )
    db.commit()
    return {"data": {"id": str(record.id), "status": record.status, "source": record.source}, "meta": None, "errors": None}


# --------------------------------------------------------------- exit extras
@router.post("/exit-requests/{exit_id}/reject", summary="Reject an exit request")
def reject_exit(
    exit_id: uuid.UUID,
    payload: DecisionIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("exit.approve")),
) -> dict:
    exit_request = get_or_404(db, ExitRequest, exit_id, "Exit request")
    if exit_request.status not in ("pending", "approved"):
        raise BusinessRuleError("Only pending or approved exits can be rejected")
    exit_request.status = "rejected"
    write_audit(
        db, user=user, entity_type="exit_requests", entity_id=exit_request.id,
        action="update", new_values={"status": "rejected", "reason": payload.reason},
        ip_address=client_ip(request),
    )
    db.commit()
    return {"data": {"status": exit_request.status}, "meta": None, "errors": None}


@router.post("/exit-requests/{exit_id}/cancel", summary="Cancel an exit request")
def cancel_exit(
    exit_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("exit.edit")),
) -> dict:
    exit_request = get_or_404(db, ExitRequest, exit_id, "Exit request")
    exit_request.status = "cancelled"
    employee = db.get(Employee, exit_request.employee_id)
    if employee is not None and employee.status == "exited":
        employee.status = "active"
        employee.date_of_exit = None
    db.commit()
    return {"data": {"status": exit_request.status}, "meta": None, "errors": None}


@router.post("/exit-requests/{exit_id}/full-and-final", summary="Compute a full & final settlement")
def full_and_final(
    exit_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("exit.approve")),
) -> dict:
    exit_request = get_or_404(db, ExitRequest, exit_id, "Exit request")
    salary = db.execute(
        select(EmployeeSalary)
        .where(EmployeeSalary.employee_id == exit_request.employee_id, EmployeeSalary.status == "active")
        .order_by(EmployeeSalary.effective_from.desc())
    ).scalars().first()
    monthly = (Decimal(salary.ctc) / Decimal(12)) if salary else Decimal("0")
    daily = monthly / Decimal(30)

    encashable = 0
    balances = db.execute(
        select(LeaveBalance, LeaveType)
        .join(LeaveType, LeaveType.id == LeaveBalance.leave_type_id)
        .where(LeaveBalance.employee_id == exit_request.employee_id, LeaveType.encashable.is_(True))
    ).all()
    encashment = Decimal("0")
    for balance, _leave_type in balances:
        encashable += 1
        encashment += Decimal(balance.available or 0) * daily

    outstanding = db.execute(
        select(func.coalesce(func.sum(Loan.outstanding), 0)).where(
            Loan.employee_id == exit_request.employee_id, Loan.status == "active"
        )
    ).scalar_one()
    loan_recovery = Decimal(outstanding or 0)
    notice_recovery = Decimal("0")
    adhoc_deductions = Decimal("0")
    net = encashment - loan_recovery - notice_recovery - adhoc_deductions
    return {
        "data": {
            "exit_request_id": str(exit_request.id),
            "employee_id": str(exit_request.employee_id),
            "monthly_ctc": str(_q(monthly)),
            "leave_encashment": str(_q(encashment)),
            "leave_types_encashable": encashable,
            "loan_recovery": str(_q(loan_recovery)),
            "notice_period_recovery": str(_q(notice_recovery)),
            "adhoc_deductions": str(_q(adhoc_deductions)),
            "net_payable": str(_q(net)),
        },
        "meta": None,
        "errors": None,
    }


# ---------------------------------------------------------------- assets extras
class AssetUnavailableIn(BaseModel):
    reason: str | None = None


@router.post("/assets/{asset_id}/unavailable", summary="Mark an asset unavailable")
def mark_asset_unavailable(
    asset_id: uuid.UUID,
    payload: AssetUnavailableIn | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("asset.edit")),
) -> dict:
    asset = get_or_404(db, Asset, asset_id, "Asset")
    if asset.status == "assigned":
        raise BusinessRuleError("Assigned assets must be returned first")
    asset.status = "unavailable"
    db.commit()
    return {"data": {"status": asset.status}, "meta": None, "errors": None}
