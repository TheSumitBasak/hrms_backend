"""Leave management tables."""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import Boolean, Date, ForeignKey, Numeric, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import IDMixin


class LeaveType(IDMixin, Base):
    __tablename__ = "leave_types"

    org_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    code: Mapped[str] = mapped_column(String(50), nullable=False)
    accrual_type: Mapped[str] = mapped_column(String(20), nullable=False)
    max_balance: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    encashable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_paid: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class LeaveBalance(IDMixin, Base):
    __tablename__ = "leave_balances"

    employee_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("employees.id"), nullable=False, index=True
    )
    leave_type_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("leave_types.id"), nullable=False
    )
    balance: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False, default=0)
    accrued: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False, default=0)
    used: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False, default=0)
    available: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False, default=0)


class LeaveRequest(IDMixin, Base):
    __tablename__ = "leave_requests"

    employee_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("employees.id"), nullable=False, index=True
    )
    leave_type_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("leave_types.id"), nullable=False
    )
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    days: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    approved_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id"))
