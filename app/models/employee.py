"""Employee core records and related profile tables."""
from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.core.types import JSONType
from app.models.base import IDMixin, TimestampMixin, utcnow


class Employee(IDMixin, TimestampMixin, Base):
    __tablename__ = "employees"

    org_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id"), nullable=False, index=True
    )
    employee_code: Mapped[str] = mapped_column(String(50), nullable=False, unique=True, index=True)
    first_name: Mapped[str] = mapped_column(String(100), nullable=False)
    last_name: Mapped[str] = mapped_column(String(100), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    personal_email: Mapped[str | None] = mapped_column(String(255))
    phone: Mapped[str | None] = mapped_column(String(20))
    date_of_birth: Mapped[date | None] = mapped_column(Date)
    gender: Mapped[str | None] = mapped_column(String(20))
    marital_status: Mapped[str | None] = mapped_column(String(20))
    date_of_joining: Mapped[date] = mapped_column(Date, nullable=False)
    date_of_exit: Mapped[date | None] = mapped_column(Date)
    employment_type: Mapped[str] = mapped_column(String(30), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="active")
    legal_entity_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("legal_entities.id"), nullable=False, index=True
    )
    department_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("departments.id"), index=True
    )
    designation_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("designations.id")
    )
    location_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("locations.id"))
    cost_center_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("cost_centers.id")
    )
    reporting_manager_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("employees.id")
    )
    pay_group_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("pay_groups.id"))
    avatar_path: Mapped[str | None] = mapped_column(String(255))


class EmployeePersonalDetail(Base):
    __tablename__ = "employee_personal_details"

    employee_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("employees.id"), primary_key=True
    )
    address_line1: Mapped[str | None] = mapped_column(String(255))
    address_line2: Mapped[str | None] = mapped_column(String(255))
    city: Mapped[str | None] = mapped_column(String(100))
    state: Mapped[str | None] = mapped_column(String(100))
    country: Mapped[str | None] = mapped_column(String(2))
    postal_code: Mapped[str | None] = mapped_column(String(20))
    emergency_contact_name: Mapped[str | None] = mapped_column(String(255))
    emergency_contact_phone: Mapped[str | None] = mapped_column(String(20))
    blood_group: Mapped[str | None] = mapped_column(String(5))
    custom_fields: Mapped[dict | None] = mapped_column(JSONType)


class EmployeeJobHistory(IDMixin, Base):
    __tablename__ = "employee_job_history"

    employee_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("employees.id"), nullable=False, index=True
    )
    effective_date: Mapped[date] = mapped_column(Date, nullable=False)
    legal_entity_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("legal_entities.id"))
    department_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("departments.id"))
    designation_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("designations.id"))
    location_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("locations.id"))
    cost_center_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("cost_centers.id"))
    reporting_manager_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("employees.id")
    )
    change_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class EmployeeBankDetail(IDMixin, Base):
    __tablename__ = "employee_bank_details"

    employee_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("employees.id"), nullable=False, index=True
    )
    account_name: Mapped[str] = mapped_column(String(255), nullable=False)
    account_number: Mapped[str] = mapped_column(String(50), nullable=False)
    bank_name: Mapped[str] = mapped_column(String(255), nullable=False)
    ifsc_code: Mapped[str | None] = mapped_column(String(20))
    branch: Mapped[str | None] = mapped_column(String(255))
    account_type: Mapped[str | None] = mapped_column(String(20))
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class EmployeeStatutoryDetail(IDMixin, TimestampMixin, Base):
    __tablename__ = "employee_statutory_details"

    employee_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("employees.id"), nullable=False, unique=True
    )
    pan: Mapped[str | None] = mapped_column(String(10))
    aadhaar: Mapped[str | None] = mapped_column(String(12))
    uan: Mapped[str | None] = mapped_column(String(12))
    pf_number: Mapped[str | None] = mapped_column(String(50))
    esic_number: Mapped[str | None] = mapped_column(String(20))
    pt_state: Mapped[str | None] = mapped_column(String(50))
    lwf_state: Mapped[str | None] = mapped_column(String(50))
    tax_regime: Mapped[str | None] = mapped_column(String(10))


class DocumentType(IDMixin, Base):
    __tablename__ = "document_types"

    org_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str | None] = mapped_column(String(50))
    is_mandatory: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    has_expiry: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class EmployeeDocument(IDMixin, Base):
    __tablename__ = "employee_documents"

    employee_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("employees.id"), nullable=False, index=True
    )
    document_type_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("document_types.id"), nullable=False
    )
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    file_path: Mapped[str] = mapped_column(Text, nullable=False)
    expiry_date: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    uploaded_by: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id"), nullable=False)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DocumentTemplate(IDMixin, Base):
    __tablename__ = "document_templates"

    org_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    type: Mapped[str] = mapped_column(String(50), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(nullable=False, default=1)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
