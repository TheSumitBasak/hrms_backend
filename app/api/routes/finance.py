"""Finance workflows: expenses, loans and tax declarations."""
from __future__ import annotations

import shutil
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import client_ip, get_current_user, require_permission, write_audit
from app.core.errors import BusinessRuleError
from app.crud.base import get_or_404, jsonable
from app.models.auth import User
from app.models.employee import Employee
from app.models.expense import ExpenseClaim, ExpenseClaimLine
from app.models.loan import Loan, LoanRepayment
from app.models.tax import TaxDeclaration, TaxProof
from app.models.workflow import Notification

router = APIRouter(tags=["Finance"])

UPLOAD_DIR = Path("uploads/finance")

MONEY = Decimal("0.01")


class DecisionIn(BaseModel):
    reason: str | None = None
    comments: str | None = None


# --------------------------------------------------------------------- Expenses
@router.get("/expense-claims/{claim_id}/detail", summary="Expense claim with lines")
def expense_claim_detail(
    claim_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("expense.view")),
) -> dict:
    claim = get_or_404(db, ExpenseClaim, claim_id, "Expense claim")
    lines = db.execute(
        select(ExpenseClaimLine).where(ExpenseClaimLine.claim_id == claim.id)
    ).scalars().all()
    return {
        "data": {
            "claim": {c.name: jsonable(getattr(claim, c.name)) for c in ExpenseClaim.__table__.columns},
            "lines": [
                {c.name: jsonable(getattr(line, c.name)) for c in ExpenseClaimLine.__table__.columns}
                for line in lines
            ],
        },
        "meta": None,
        "errors": None,
    }


def _recalc_claim_total(db: Session, claim: ExpenseClaim) -> Decimal:
    total = db.execute(
        select(ExpenseClaimLine).where(ExpenseClaimLine.claim_id == claim.id)
    ).scalars().all()
    amount = sum((line.amount for line in total), Decimal(0))
    claim.total_amount = amount
    return amount


@router.post("/expense-claims/{claim_id}/submit", summary="Submit an expense claim")
def submit_expense_claim(
    claim_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("expense.edit")),
) -> dict:
    claim = get_or_404(db, ExpenseClaim, claim_id, "Expense claim")
    if claim.status not in ("draft", "rejected"):
        raise BusinessRuleError("Only draft or rejected claims can be submitted")
    _recalc_claim_total(db, claim)
    claim.status = "submitted"
    claim.submitted_at = datetime.now(timezone.utc)
    write_audit(
        db,
        user=user,
        entity_type="expense_claims",
        entity_id=claim.id,
        action="update",
        new_values={"status": "submitted", "total": jsonable(claim.total_amount)},
        ip_address=client_ip(request),
    )
    db.commit()
    return {"data": {"status": claim.status}, "meta": None, "errors": None}


@router.post("/expense-claims/{claim_id}/approve", summary="Approve an expense claim")
def approve_expense_claim(
    claim_id: uuid.UUID,
    payload: DecisionIn | None = None,
    request: Request = None,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("expense.approve")),
) -> dict:
    claim = get_or_404(db, ExpenseClaim, claim_id, "Expense claim")
    if claim.status not in ("submitted", "pending"):
        raise BusinessRuleError("Only submitted claims can be approved")
    claim.status = "approved"
    claim.approved_by = user.id
    write_audit(
        db,
        user=user,
        entity_type="expense_claims",
        entity_id=claim.id,
        action="update",
        new_values={"status": "approved"},
        ip_address=client_ip(request),
    )
    db.commit()
    return {"data": {"status": claim.status}, "meta": None, "errors": None}


@router.post("/expense-claims/{claim_id}/reject", summary="Reject an expense claim")
def reject_expense_claim(
    claim_id: uuid.UUID,
    payload: DecisionIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("expense.approve")),
) -> dict:
    claim = get_or_404(db, ExpenseClaim, claim_id, "Expense claim")
    claim.status = "rejected"
    write_audit(
        db,
        user=user,
        entity_type="expense_claims",
        entity_id=claim.id,
        action="update",
        new_values={"status": "rejected", "reason": payload.reason},
        ip_address=client_ip(request),
    )
    db.commit()
    return {"data": {"status": claim.status}, "meta": None, "errors": None}


class MarkPaidIn(BaseModel):
    payroll_run_id: uuid.UUID | None = None


@router.post("/expense-claims/{claim_id}/mark-paid", summary="Mark an expense claim as paid")
def mark_expense_claim_paid(
    claim_id: uuid.UUID,
    payload: MarkPaidIn | None = None,
    request: Request = None,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("expense.approve")),
) -> dict:
    claim = get_or_404(db, ExpenseClaim, claim_id, "Expense claim")
    if claim.status != "approved":
        raise BusinessRuleError("Only approved claims can be marked paid")
    claim.status = "paid"
    claim.paid_in_payroll_run_id = payload.payroll_run_id if payload else None
    write_audit(
        db,
        user=user,
        entity_type="expense_claims",
        entity_id=claim.id,
        action="update",
        new_values={"status": "paid"},
        ip_address=client_ip(request),
    )
    db.commit()
    return {"data": {"status": claim.status}, "meta": None, "errors": None}


# ------------------------------------------------------------------------ Loans
def _emi(principal: Decimal, annual_rate: Decimal, months: int) -> Decimal:
    if months <= 0:
        return principal
    if annual_rate == 0:
        return (principal / Decimal(months)).quantize(MONEY)
    r = (annual_rate / Decimal(100)) / Decimal(12)
    factor = (Decimal(1) + r) ** months
    emi = principal * r * factor / (factor - Decimal(1))
    return emi.quantize(MONEY)


class LoanApplicationIn(BaseModel):
    employee_id: uuid.UUID
    loan_policy_id: uuid.UUID | None = None
    principal: Decimal
    interest_rate: Decimal = Decimal("0")
    tenure_months: int = Field(gt=0)
    disbursement_date: date | None = None


@router.post("/loans/apply", status_code=201, summary="Apply for a loan")
def apply_loan(
    payload: LoanApplicationIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("loan.create")),
) -> dict:
    emi = _emi(payload.principal, payload.interest_rate, payload.tenure_months)
    loan = Loan(
        employee_id=payload.employee_id,
        loan_policy_id=payload.loan_policy_id,
        principal=payload.principal,
        interest_rate=payload.interest_rate,
        tenure_months=payload.tenure_months,
        emi_amount=emi,
        outstanding=payload.principal,
        status="pending",
    )
    db.add(loan)
    write_audit(
        db,
        user=user,
        entity_type="loans",
        entity_id=payload.employee_id,
        action="create",
        new_values={**payload.model_dump(), "emi_amount": jsonable(emi)},
        ip_address=client_ip(request),
    )
    db.commit()
    db.refresh(loan)
    return {
        "data": {"id": str(loan.id), "emi_amount": jsonable(emi), "status": loan.status},
        "meta": None,
        "errors": None,
    }


@router.post("/loans/{loan_id}/approve", summary="Approve a loan and build the EMI schedule")
def approve_loan(
    loan_id: uuid.UUID,
    payload: DecisionIn | None = None,
    request: Request = None,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("loan.approve")),
) -> dict:
    loan = get_or_404(db, Loan, loan_id, "Loan")
    if loan.status not in ("pending", "draft"):
        raise BusinessRuleError("Only pending loans can be approved")
    loan.status = "active"
    loan.disbursed_at = datetime.now(timezone.utc)
    db.query(LoanRepayment).filter(LoanRepayment.loan_id == loan.id).delete()
    balance = Decimal(loan.principal)
    r = (Decimal(loan.interest_rate) / Decimal(100)) / Decimal(12)
    start = date.today().replace(day=1)
    for index in range(loan.tenure_months):
        interest = (balance * r).quantize(MONEY)
        principal_component = (Decimal(loan.emi_amount) - interest).quantize(MONEY)
        if index == loan.tenure_months - 1:
            principal_component = balance.quantize(MONEY)
        balance = (balance - principal_component).quantize(MONEY)
        month = (start.month - 1 + index) % 12 + 1
        year = start.year + (start.month - 1 + index) // 12
        db.add(
            LoanRepayment(
                loan_id=loan.id,
                due_date=date(year, month, 1),
                emi_amount=loan.emi_amount,
                principal_component=principal_component,
                interest_component=interest,
                status="pending",
            )
        )
    write_audit(
        db,
        user=user,
        entity_type="loans",
        entity_id=loan.id,
        action="update",
        new_values={"status": "active"},
        ip_address=client_ip(request),
    )
    db.commit()
    return {"data": {"status": loan.status}, "meta": None, "errors": None}


@router.get("/loans/{loan_id}/schedule", summary="Loan amortization schedule")
def loan_schedule(
    loan_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("loan.view")),
) -> dict:
    get_or_404(db, Loan, loan_id, "Loan")
    rows = db.execute(
        select(LoanRepayment)
        .where(LoanRepayment.loan_id == loan_id)
        .order_by(LoanRepayment.due_date)
    ).scalars().all()
    return {
        "data": [
            {c.name: jsonable(getattr(row, c.name)) for c in LoanRepayment.__table__.columns}
            for row in rows
        ],
        "meta": None,
        "errors": None,
    }


@router.post("/loans/{loan_id}/skip-emi", summary="Skip an EMI instalment")
def skip_emi(
    loan_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("loan.approve")),
    due_date: date | None = None,
) -> dict:
    loan = get_or_404(db, Loan, loan_id, "Loan")
    query = select(LoanRepayment).where(
        LoanRepayment.loan_id == loan.id, LoanRepayment.status == "pending"
    )
    if due_date:
        query = query.where(LoanRepayment.due_date == due_date)
    instalment = db.execute(query.order_by(LoanRepayment.due_date)).scalars().first()
    if instalment is None:
        raise BusinessRuleError("No pending instalment found")
    instalment.status = "skipped"
    write_audit(
        db,
        user=user,
        entity_type="loans",
        entity_id=loan.id,
        action="update",
        new_values={"skipped_instalment": str(instalment.id)},
        ip_address=client_ip(request),
    )
    db.commit()
    return {"data": {"status": instalment.status}, "meta": None, "errors": None}


class ForecloseIn(BaseModel):
    reason: str | None = None


@router.post("/loans/{loan_id}/foreclose", summary="Foreclose a loan")
def foreclose_loan(
    loan_id: uuid.UUID,
    payload: ForecloseIn | None = None,
    request: Request = None,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("loan.approve")),
) -> dict:
    loan = get_or_404(db, Loan, loan_id, "Loan")
    loan.status = "closed"
    loan.outstanding = Decimal("0.00")
    db.query(LoanRepayment).filter(
        LoanRepayment.loan_id == loan.id, LoanRepayment.status == "pending"
    ).delete(synchronize_session=False)
    write_audit(
        db,
        user=user,
        entity_type="loans",
        entity_id=loan.id,
        action="update",
        new_values={"status": "closed", "reason": payload.reason if payload else None},
        ip_address=client_ip(request),
    )
    db.commit()
    return {"data": {"status": loan.status}, "meta": None, "errors": None}


# --------------------------------------------------------------- Tax declarations
@router.get("/tax-declarations/{declaration_id}/detail", summary="Declaration with proofs")
def tax_declaration_detail(
    declaration_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("tax_declaration.view")),
) -> dict:
    declaration = get_or_404(db, TaxDeclaration, declaration_id, "Tax declaration")
    proofs = db.execute(
        select(TaxProof).where(TaxProof.declaration_id == declaration.id)
    ).scalars().all()
    return {
        "data": {
            "declaration": {
                c.name: jsonable(getattr(declaration, c.name))
                for c in TaxDeclaration.__table__.columns
            },
            "proofs": [
                {c.name: jsonable(getattr(proof, c.name)) for c in TaxProof.__table__.columns}
                for proof in proofs
            ],
        },
        "meta": None,
        "errors": None,
    }


@router.post("/tax-declarations/{declaration_id}/submit", summary="Submit a tax declaration")
def submit_tax_declaration(
    declaration_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("tax_declaration.edit")),
) -> dict:
    declaration = get_or_404(db, TaxDeclaration, declaration_id, "Tax declaration")
    declaration.status = "submitted"
    declaration.submitted_at = datetime.now(timezone.utc)
    write_audit(
        db,
        user=user,
        entity_type="tax_declarations",
        entity_id=declaration.id,
        action="update",
        new_values={"status": "submitted"},
        ip_address=client_ip(request),
    )
    db.commit()
    return {"data": {"status": declaration.status}, "meta": None, "errors": None}


@router.post("/tax-declarations/{declaration_id}/approve", summary="Approve a tax declaration")
def approve_tax_declaration(
    declaration_id: uuid.UUID,
    payload: DecisionIn | None = None,
    request: Request = None,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("tax_declaration.approve")),
) -> dict:
    declaration = get_or_404(db, TaxDeclaration, declaration_id, "Tax declaration")
    declaration.status = "approved"
    declaration.approved_by = user.id
    write_audit(
        db,
        user=user,
        entity_type="tax_declarations",
        entity_id=declaration.id,
        action="update",
        new_values={"status": "approved"},
        ip_address=client_ip(request),
    )
    db.commit()
    return {"data": {"status": declaration.status}, "meta": None, "errors": None}


@router.post("/tax-declarations/{declaration_id}/reject", summary="Reject a tax declaration")
def reject_tax_declaration(
    declaration_id: uuid.UUID,
    payload: DecisionIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("tax_declaration.approve")),
) -> dict:
    declaration = get_or_404(db, TaxDeclaration, declaration_id, "Tax declaration")
    declaration.status = "rejected"
    write_audit(
        db,
        user=user,
        entity_type="tax_declarations",
        entity_id=declaration.id,
        action="update",
        new_values={"status": "rejected", "reason": payload.reason},
        ip_address=client_ip(request),
    )
    db.commit()
    return {"data": {"status": declaration.status}, "meta": None, "errors": None}


class ProofVerifyIn(BaseModel):
    status: str = Field(pattern="^(approved|rejected|pending)$")


@router.post("/tax-proofs/{proof_id}/verify", summary="Verify a tax proof")
def verify_tax_proof(
    proof_id: uuid.UUID,
    payload: ProofVerifyIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("tax_proof.approve")),
) -> dict:
    proof = get_or_404(db, TaxProof, proof_id, "Tax proof")
    proof.status = payload.status
    proof.verified_by = user.id
    write_audit(
        db,
        user=user,
        entity_type="tax_proofs",
        entity_id=proof.id,
        action="update",
        new_values={"status": payload.status},
        ip_address=client_ip(request),
    )
    db.commit()
    return {"data": {"status": proof.status}, "meta": None, "errors": None}


def _notify_employee(db: Session, employee_id, title: str, body: str, actor: User | None = None) -> bool:
    from app.models.auth import User as UserModel
    from app.models.employee import Employee as EmployeeModel

    employee = db.get(EmployeeModel, employee_id)
    if employee is None:
        return False
    user = db.execute(select(UserModel).where(UserModel.employee_id == employee.id)).scalar_one_or_none()
    if user is None:
        return False
    db.add(
        Notification(
            user_id=user.id,
            title=title,
            body=body,
            channel="email",
            status="queued",
            sent_at=datetime.now(timezone.utc),
        )
    )
    return True


def _store_upload(file: UploadFile, prefix: str) -> str:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    suffix = Path(file.filename or "").suffix
    destination = UPLOAD_DIR / f"{prefix}_{uuid.uuid4()}{suffix}"
    with destination.open("wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
    if destination.stat().st_size > 10 * 1024 * 1024:
        destination.unlink(missing_ok=True)
        raise BusinessRuleError("File exceeds the 10MB limit")
    return str(destination)


# ------------------------------------------------------------ expense receipts
@router.post("/expense-claim-lines/{line_id}/receipt", summary="Attach a receipt to a claim line")
def upload_receipt(
    line_id: uuid.UUID,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("expense.edit")),
) -> dict:
    line = get_or_404(db, ExpenseClaimLine, line_id, "Expense claim line")
    line.receipt_path = _store_upload(file, f"receipt_{line.id}")
    db.add(line)
    db.commit()
    return {"data": {"line_id": str(line.id), "receipt_path": line.receipt_path}, "meta": None, "errors": None}


@router.get("/expense-claim-lines/{line_id}/receipt", summary="Download a claim line receipt")
def download_receipt(
    line_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("expense.view")),
):
    line = get_or_404(db, ExpenseClaimLine, line_id, "Expense claim line")
    if not line.receipt_path:
        raise BusinessRuleError("No receipt attached")
    path = Path(line.receipt_path)
    if not path.exists():
        raise BusinessRuleError("Stored receipt not found")
    return FileResponse(path, filename=path.name)


@router.post("/expense-claims/{claim_id}/unpaid", summary="Reverse a paid expense claim")
def unpay_expense_claim(
    claim_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("expense.approve")),
) -> dict:
    claim = get_or_404(db, ExpenseClaim, claim_id, "Expense claim")
    if claim.status != "paid":
        raise BusinessRuleError("Only paid claims can be reversed")
    claim.status = "approved"
    claim.paid_in_payroll_run_id = None
    write_audit(
        db, user=user, entity_type="expense_claims", entity_id=claim.id,
        action="update", new_values={"status": "approved", "reversed": True},
        ip_address=client_ip(request),
    )
    db.commit()
    return {"data": {"status": claim.status}, "meta": None, "errors": None}


@router.post("/expense-claims/{claim_id}/remind", summary="Send a reminder for an expense claim")
def remind_expense_claim(
    claim_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("expense.view")),
) -> dict:
    claim = get_or_404(db, ExpenseClaim, claim_id, "Expense claim")
    sent = _notify_employee(
        db, claim.employee_id, "Reminder: expense claim pending",
        f"Your expense claim {claim.id} is awaiting action.", user,
    )
    db.commit()
    return {"data": {"queued": sent}, "meta": None, "errors": None}


# ---------------------------------------------------------------- tax proofs
@router.post("/tax-proofs/{proof_id}/upload", summary="Upload a tax proof file")
def upload_tax_proof(
    proof_id: uuid.UUID,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("tax_proof.edit")),
) -> dict:
    proof = get_or_404(db, TaxProof, proof_id, "Tax proof")
    proof.proof_file_path = _store_upload(file, f"proof_{proof.id}")
    proof.status = "pending"
    db.add(proof)
    db.commit()
    return {"data": {"proof_id": str(proof.id), "proof_file_path": proof.proof_file_path}, "meta": None, "errors": None}


@router.get("/tax-proofs/{proof_id}/file", summary="Download a tax proof file")
def download_tax_proof(
    proof_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("tax_proof.view")),
):
    proof = get_or_404(db, TaxProof, proof_id, "Tax proof")
    if not proof.proof_file_path:
        raise BusinessRuleError("No proof file uploaded")
    path = Path(proof.proof_file_path)
    if not path.exists():
        raise BusinessRuleError("Stored proof not found")
    return FileResponse(path, filename=path.name)


@router.post("/tax-declarations/{declaration_id}/remind", summary="Remind an employee to submit declarations")
def remind_tax_declaration(
    declaration_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("tax_declaration.view")),
) -> dict:
    declaration = get_or_404(db, TaxDeclaration, declaration_id, "Tax declaration")
    sent = _notify_employee(
        db, declaration.employee_id, "Reminder: tax declaration",
        f"Please complete your tax declaration for FY {declaration.financial_year}.", user,
    )
    db.commit()
    return {"data": {"queued": sent}, "meta": None, "errors": None}


# --------------------------------------------------------------------- loans
@router.post("/loans/{loan_id}/mark-paid-outside-payroll", summary="Mark a loan settled outside payroll")
def mark_loan_paid_outside(
    loan_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("loan.approve")),
) -> dict:
    loan = get_or_404(db, Loan, loan_id, "Loan")
    loan.paid_outside_payroll = True
    loan.status = "closed"
    loan.outstanding = Decimal("0.00")
    db.query(LoanRepayment).filter(
        LoanRepayment.loan_id == loan.id, LoanRepayment.status == "pending"
    ).delete(synchronize_session=False)
    write_audit(
        db, user=user, entity_type="loans", entity_id=loan.id,
        action="update", new_values={"status": "closed", "paid_outside_payroll": True},
        ip_address=client_ip(request),
    )
    db.commit()
    return {"data": {"status": loan.status, "paid_outside_payroll": loan.paid_outside_payroll}, "meta": None, "errors": None}


@router.post("/loans/{loan_id}/remind", summary="Send a loan repayment reminder")
def remind_loan(
    loan_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("loan.view")),
) -> dict:
    loan = get_or_404(db, Loan, loan_id, "Loan")
    sent = _notify_employee(
        db, loan.employee_id, "Reminder: loan instalment",
        f"Your loan {loan.id} has an outstanding balance of {loan.outstanding}.", user,
    )
    db.commit()
    return {"data": {"queued": sent}, "meta": None, "errors": None}
