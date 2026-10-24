"""Workflow engine, approvals, notifications and the unified inbox."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_current_user, require_permission, write_audit
from app.core.errors import BusinessRuleError
from app.crud.base import get_or_404, jsonable
from app.models.auth import User
from app.models.workflow import (
    Approval,
    HelpdeskTicket,
    Notification,
    Workflow,
    WorkflowStep,
)
from app.models.leave import LeaveRequest
from app.models.expense import ExpenseClaim
from app.models.loan import Loan
from app.models.lifecycle import ExitRequest

router = APIRouter(tags=["Workflows"])


class StartWorkflowIn(BaseModel):
    entity_type: str
    entity_id: uuid.UUID


class ApprovalActionIn(BaseModel):
    comments: str | None = None


@router.post("/workflows/{workflow_id}/start", status_code=201, summary="Start a workflow instance")
def start_workflow(
    workflow_id: uuid.UUID,
    payload: StartWorkflowIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("workflow.edit")),
) -> dict:
    workflow = get_or_404(db, Workflow, workflow_id, "Workflow")
    steps = db.execute(
        select(WorkflowStep)
        .where(WorkflowStep.workflow_id == workflow.id)
        .order_by(WorkflowStep.step_order)
    ).scalars().all()
    if not steps:
        raise BusinessRuleError("Workflow has no steps configured")
    created = []
    for step in steps:
        approval = Approval(
            workflow_id=workflow.id,
            entity_type=payload.entity_type,
            entity_id=payload.entity_id,
            step_order=step.step_order,
            status="pending",
        )
        db.add(approval)
        created.append(approval)
    write_audit(
        db,
        user=user,
        entity_type="workflows",
        entity_id=workflow.id,
        action="create",
        new_values={"entity_type": payload.entity_type, "entity_id": str(payload.entity_id)},
    )
    db.commit()
    return {"data": {"approvals": len(created)}, "meta": None, "errors": None}


@router.get("/approvals/pending", summary="Pending approvals for the current user")
def pending_approvals(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    rows = db.execute(
        select(Approval)
        .where(Approval.status == "pending", (Approval.approver_id == user.id) | (Approval.approver_id.is_(None)))
        .order_by(Approval.step_order)
    ).scalars().all()
    return {
        "data": [
            {
                "id": str(a.id),
                "workflow_id": str(a.workflow_id),
                "entity_type": a.entity_type,
                "entity_id": str(a.entity_id),
                "step_order": a.step_order,
                "status": a.status,
            }
            for a in rows
        ],
        "meta": {"total": len(rows)},
        "errors": None,
    }


@router.post("/approvals/{approval_id}/approve", summary="Approve an approval step")
def approve_step(
    approval_id: uuid.UUID,
    payload: ApprovalActionIn | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    approval = get_or_404(db, Approval, approval_id, "Approval")
    if approval.status != "pending":
        raise BusinessRuleError("Approval has already been actioned")
    approval.status = "approved"
    approval.approver_id = user.id
    approval.acted_at = datetime.now(timezone.utc)
    approval.comments = payload.comments if payload else None
    db.commit()
    return {"data": {"status": approval.status}, "meta": None, "errors": None}


@router.post("/approvals/{approval_id}/reject", summary="Reject an approval step")
def reject_step(
    approval_id: uuid.UUID,
    payload: ApprovalActionIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    approval = get_or_404(db, Approval, approval_id, "Approval")
    approval.status = "rejected"
    approval.approver_id = user.id
    approval.acted_at = datetime.now(timezone.utc)
    approval.comments = payload.comments
    db.commit()
    return {"data": {"status": approval.status}, "meta": None, "errors": None}


@router.get("/inbox", summary="Unified inbox of pending actions")
def inbox(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    def _count(model, status_value="pending"):
        return int(
            db.execute(
                select(func.count()).select_from(model).where(model.status == status_value)
            ).scalar_one()
        )

    items = [
        {"module": "leave", "title": "Leave approvals", "count": _count(LeaveRequest)},
        {"module": "expense", "title": "Expense approvals", "count": _count(ExpenseClaim, "submitted")},
        {"module": "loan", "title": "Loan approvals", "count": _count(Loan)},
        {"module": "exit", "title": "Exit approvals", "count": _count(ExitRequest)},
        {
            "module": "helpdesk",
            "title": "Helpdesk assignments",
            "count": int(
                db.execute(
                    select(func.count())
                    .select_from(HelpdeskTicket)
                    .where(HelpdeskTicket.status.in_(["open", "in_progress"]))
                ).scalar_one()
            ),
        },
        {
            "module": "approval",
            "title": "Workflow approvals",
            "count": int(
                db.execute(
                    select(func.count())
                    .select_from(Approval)
                    .where(
                        Approval.status == "pending",
                        (Approval.approver_id == user.id) | (Approval.approver_id.is_(None)),
                    )
                ).scalar_one()
            ),
        },
    ]
    return {"data": items, "meta": {"total": sum(i["count"] for i in items)}, "errors": None}


@router.get("/notifications/my", summary="Current user's notifications")
def my_notifications(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    rows = db.execute(
        select(Notification)
        .where(Notification.user_id == user.id)
        .order_by(Notification.sent_at.desc())
        .limit(50)
    ).scalars().all()
    return {
        "data": [
            {
                "id": str(n.id),
                "title": n.title,
                "body": n.body,
                "channel": n.channel,
                "status": n.status,
                "sent_at": jsonable(n.sent_at),
                "read_at": jsonable(n.read_at),
            }
            for n in rows
        ],
        "meta": {"total": len(rows)},
        "errors": None,
    }


@router.post("/notifications/{notification_id}/read", summary="Mark a notification read")
def mark_notification_read(
    notification_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    notification = get_or_404(db, Notification, notification_id, "Notification")
    notification.status = "read"
    notification.read_at = datetime.now(timezone.utc)
    db.commit()
    return {"data": {"status": notification.status}, "meta": None, "errors": None}


class RequestInfoIn(BaseModel):
    comments: str


@router.post("/approvals/{approval_id}/request-info", summary="Request more information on an approval")
def request_info(
    approval_id: uuid.UUID,
    payload: RequestInfoIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    approval = get_or_404(db, Approval, approval_id, "Approval")
    if approval.status != "pending":
        raise BusinessRuleError("Only pending approvals can be queried")
    approval.status = "info_requested"
    approval.approver_id = user.id
    approval.acted_at = datetime.now(timezone.utc)
    approval.comments = payload.comments
    db.commit()
    return {"data": {"status": approval.status}, "meta": None, "errors": None}


@router.post("/notifications/read-all", summary="Mark all of the current user's notifications read")
def mark_all_read(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    rows = db.execute(
        select(Notification).where(Notification.user_id == user.id, Notification.status != "read")
    ).scalars().all()
    now = datetime.now(timezone.utc)
    for row in rows:
        row.status = "read"
        row.read_at = now
        db.add(row)
    db.commit()
    return {"data": {"updated": len(rows)}, "meta": None, "errors": None}


@router.get("/notifications/unread-count", summary="Unread notification count")
def unread_count(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    count = int(
        db.execute(
            select(func.count())
            .select_from(Notification)
            .where(Notification.user_id == user.id, Notification.status != "read")
        ).scalar_one()
    )
    return {"data": {"unread": count}, "meta": None, "errors": None}


@router.get("/inbox/me", summary="Inbox scoped to the current user")
def inbox_me(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    my_approvals = db.execute(
        select(Approval).where(
            Approval.status == "pending",
            (Approval.approver_id == user.id) | (Approval.approver_id.is_(None)),
        )
    ).scalars().all()
    my_tickets = []
    if user.employee_id:
        my_tickets = db.execute(
            select(HelpdeskTicket).where(
                HelpdeskTicket.employee_id == user.employee_id,
                HelpdeskTicket.status != "closed",
            )
        ).scalars().all()
    items = [
        {
            "module": "approval",
            "id": str(a.id),
            "title": f"{a.entity_type} approval (step {a.step_order})",
            "entity_type": a.entity_type,
            "entity_id": str(a.entity_id),
            "status": a.status,
        }
        for a in my_approvals
    ]
    items += [
        {
            "module": "helpdesk",
            "id": str(t.id),
            "title": t.subject,
            "status": t.status,
        }
        for t in my_tickets
    ]
    return {"data": items, "meta": {"total": len(items)}, "errors": None}
