"""Payroll setup, structure and run tables."""
from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.core.types import JSONType
from app.models.base import IDMixin, TimestampMixin, utcnow


class PayGroup(IDMixin, Base):
    __tablename__ = "pay_groups"

    org_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id"), nullable=False, index=True
    )
    legal_entity_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("legal_entities.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    pay_cycle: Mapped[str] = mapped_column(String(20), nullable=False)
    cut_off_day: Mapped[int | None] = mapped_column(Integer)
    pay_date: Mapped[int | None] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class SalaryStructure(IDMixin, TimestampMixin, Base):
    __tablename__ = "salary_structures"

    org_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    code: Mapped[str] = mapped_column(String(50), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class SalaryComponent(IDMixin, Base):
    __tablename__ = "salary_components"

    org_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    code: Mapped[str] = mapped_column(String(50), nullable=False)
    component_type: Mapped[str] = mapped_column(String(20), nullable=False)
    calculation_type: Mapped[str] = mapped_column(String(20), nullable=False)
    formula: Mapped[str | None] = mapped_column(Text)
    taxable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_statutory: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_fbp: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class SalaryStructureComponent(IDMixin, Base):
    __tablename__ = "salary_structure_components"

    structure_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("salary_structures.id"), nullable=False, index=True
    )
    component_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("salary_components.id"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    is_mandatory: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    default_value: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    formula_override: Mapped[str | None] = mapped_column(Text)


class EmployeeSalary(IDMixin, Base):
    __tablename__ = "employee_salaries"

    employee_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("employees.id"), nullable=False, index=True
    )
    structure_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("salary_structures.id"), nullable=False
    )
    ctc: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SalaryRevision(IDMixin, Base):
    __tablename__ = "salary_revisions"

    employee_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("employees.id"), nullable=False, index=True
    )
    old_ctc: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    new_ctc: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    effective_date: Mapped[date] = mapped_column(Date, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    approved_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id"))
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PayrollRun(IDMixin, Base):
    __tablename__ = "payroll_runs"

    org_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id"), nullable=False, index=True
    )
    legal_entity_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("legal_entities.id"), nullable=False
    )
    pay_group_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("pay_groups.id"), nullable=False
    )
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    pay_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft")
    run_type: Mapped[str] = mapped_column(String(20), nullable=False, default="regular")
    created_by: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id"), nullable=False)
    approved_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id"))
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PayrollRunEmployee(IDMixin, Base):
    __tablename__ = "payroll_run_employees"

    payroll_run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("payroll_runs.id"), nullable=False, index=True
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("employees.id"), nullable=False
    )
    gross: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=0)
    deductions: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=0)
    net_pay: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    error_message: Mapped[str | None] = mapped_column(Text)


class Payslip(IDMixin, Base):
    __tablename__ = "payslips"

    payroll_run_employee_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("payroll_run_employees.id"), nullable=False
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("employees.id"), nullable=False, index=True
    )
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    gross: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    total_deductions: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    net_pay: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pdf_path: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft")


class PayslipLine(IDMixin, Base):
    __tablename__ = "payslip_lines"

    payslip_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("payslips.id"), nullable=False, index=True
    )
    component_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("salary_components.id")
    )
    component_name: Mapped[str] = mapped_column(String(255), nullable=False)
    type: Mapped[str] = mapped_column(String(20), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)


class StatutorySetting(IDMixin, Base):
    __tablename__ = "statutory_settings"

    legal_entity_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("legal_entities.id"), nullable=False, index=True
    )
    country_code: Mapped[str] = mapped_column(String(2), nullable=False)
    pf_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    esi_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    pt_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    lwf_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    tds_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    settings: Mapped[dict | None] = mapped_column(JSONType)
