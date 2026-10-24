"""Income-tax declarations and proofs."""
from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Numeric, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import IDMixin, utcnow


class TaxDeclaration(IDMixin, Base):
    __tablename__ = "tax_declarations"

    employee_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("employees.id"), nullable=False, index=True
    )
    financial_year: Mapped[str] = mapped_column(String(9), nullable=False)
    regime: Mapped[str] = mapped_column(String(10), nullable=False)
    total_declared: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft")
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id"))


class TaxProof(IDMixin, Base):
    __tablename__ = "tax_proofs"

    declaration_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("tax_declarations.id"), nullable=False, index=True
    )
    section: Mapped[str] = mapped_column(String(10), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    proof_file_path: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    verified_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id"))
