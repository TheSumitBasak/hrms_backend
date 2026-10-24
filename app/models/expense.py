"""Expense policies, claims and claim lines."""
from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Numeric, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.core.types import JSONType
from app.models.base import IDMixin, utcnow


class ExpensePolicy(IDMixin, Base):
    __tablename__ = "expense_policies"

    org_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    max_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    requires_receipt: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    approval_chain: Mapped[dict | None] = mapped_column(JSONType)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class ExpenseClaim(IDMixin, Base):
    __tablename__ = "expense_claims"

    employee_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("employees.id"), nullable=False, index=True
    )
    policy_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("expense_policies.id"))
    claim_date: Mapped[date] = mapped_column(Date, nullable=False)
    total_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft")
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id"))
    paid_in_payroll_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("payroll_runs.id")
    )


class ExpenseClaimLine(IDMixin, Base):
    __tablename__ = "expense_claim_lines"

    claim_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("expense_claims.id"), nullable=False, index=True
    )
    category: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    receipt_path: Mapped[str | None] = mapped_column(Text)
    expense_date: Mapped[date] = mapped_column(Date, nullable=False)
