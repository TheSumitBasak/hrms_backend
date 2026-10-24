"""Time & attendance: shifts, attendance records and holidays."""
from __future__ import annotations

import uuid
from datetime import date, datetime, time
from decimal import Decimal

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, Numeric, String, Time, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import IDMixin


class Shift(IDMixin, Base):
    __tablename__ = "shifts"

    org_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    start_time: Mapped[time] = mapped_column(Time, nullable=False)
    end_time: Mapped[time] = mapped_column(Time, nullable=False)
    break_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_night_shift: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class Holiday(IDMixin, Base):
    __tablename__ = "holidays"

    org_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id"), nullable=False, index=True
    )
    location_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("locations.id"))
    date: Mapped[date] = mapped_column(Date, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    is_optional: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class AttendanceRecord(IDMixin, Base):
    __tablename__ = "attendance_records"

    employee_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("employees.id"), nullable=False, index=True
    )
    date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    shift_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("shifts.id"))
    clock_in: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    clock_out: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    worked_hours: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    overtime_hours: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="present")
    source: Mapped[str | None] = mapped_column(String(20))
