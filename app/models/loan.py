"""Loan policies, loans and repayment schedules."""
from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, Numeric, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import IDMixin


class LoanPolicy(IDMixin, Base):
    __tablename__ = "loan_policies"

    org_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    interest_rate: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False, default=0)
    max_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    max_tenure_months: Mapped[int | None] = mapped_column(Integer)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class Loan(IDMixin, Base):
    __tablename__ = "loans"

    employee_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("employees.id"), nullable=False, index=True
    )
    loan_policy_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("loan_policies.id"))
    principal: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    interest_rate: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    tenure_months: Mapped[int] = mapped_column(Integer, nullable=False)
    emi_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    outstanding: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    paid_outside_payroll: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    disbursed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LoanRepayment(IDMixin, Base):
    __tablename__ = "loan_repayments"

    loan_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("loans.id"), nullable=False, index=True
    )
    payroll_run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("payroll_runs.id"))
    due_date: Mapped[date] = mapped_column(Date, nullable=False)
    emi_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    principal_component: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    interest_component: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
