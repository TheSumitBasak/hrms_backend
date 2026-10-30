"""Branches and work locations (§8).

Three fields the §8.1 table lists — ``gstin``, ``shops_establishments_registration_number``
and ``factory_license_number`` — are **not columns here**. §8.1's own design decision stores
them once in ``organization_registrations`` with a ``branch_id`` and exposes them on the branch
API as derived read-only values; the create/edit endpoints accept them as a convenience and the
service writes the registration row in the same transaction (DEC-015). Duplicating them as
columns would create two sources of truth for the same statutory number.
"""

from __future__ import annotations

import uuid

from sqlalchemy import CheckConstraint, Computed, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.mixins import StandardColumns, archive_reason_constraint, status_constraint
from app.db.types import Uuid7


class Branch(Base, StandardColumns):
    """A physical location belonging to exactly one organization (§8.1).

    ``organization_id`` is immutable (§8.2) and ``branch_code`` becomes immutable once an
    employee references it. Branches referenced by any employee, job-history row, registration
    or work location are never deleted — they are set ``INACTIVE`` (§8.2), which is why the
    model has no delete path and the service refuses one.
    """

    __tablename__ = "branches"
    __table_args__ = (
        UniqueConstraint("organization_id", "branch_code_normalized", name="uq_branches_org_code"),
        UniqueConstraint("organization_id", "id", name="uq_branches_org_id"),
        CheckConstraint(
            "branch_code REGEXP '^[A-Z0-9][A-Z0-9_./-]{0,29}$'", name="ck_branches_code_pattern"
        ),
        CheckConstraint(
            "CHAR_LENGTH(branch_name) BETWEEN 2 AND 150", name="ck_branches_name_length"
        ),
        CheckConstraint(
            "CHAR_LENGTH(address_line_1) BETWEEN 3 AND 200", name="ck_branches_address_line_1"
        ),
        CheckConstraint("CHAR_LENGTH(city) BETWEEN 2 AND 100", name="ck_branches_city"),
        # §8.1: state_code is required when the country is India, where it drives the default
        # professional-tax and labour-welfare-fund state on an employee record (§11.4).
        CheckConstraint(
            "country <> 'IN' OR state_code IS NOT NULL", name="ck_branches_state_code_for_india"
        ),
        status_constraint("branches"),
        archive_reason_constraint("branches"),
        Index("ix_branches_org_status", "organization_id", "status"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid7, ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    branch_code: Mapped[str] = mapped_column(String(30), nullable=False)
    branch_code_normalized: Mapped[str] = mapped_column(
        String(30), Computed("UPPER(branch_code)", persisted=True), nullable=False
    )
    branch_name: Mapped[str] = mapped_column(String(150), nullable=False)

    # §8.1 address, inline rather than prefixed: the branch *is* the address.
    address_line_1: Mapped[str] = mapped_column(String(200), nullable=False)
    address_line_2: Mapped[str | None] = mapped_column(String(200), nullable=True)
    city: Mapped[str] = mapped_column(String(100), nullable=False)
    district: Mapped[str | None] = mapped_column(String(100), nullable=True)
    state: Mapped[str] = mapped_column(String(100), nullable=False)
    state_code: Mapped[str | None] = mapped_column(String(2), nullable=True)
    country: Mapped[str] = mapped_column(String(2), nullable=False, default="IN")
    postal_code: Mapped[str] = mapped_column(String(10), nullable=False)

    official_phone: Mapped[str | None] = mapped_column(String(16), nullable=True)
    official_email: Mapped[str | None] = mapped_column(String(254), nullable=True)

    status: Mapped[str] = mapped_column(String(10), nullable=False, default="ACTIVE")

    @property
    def is_assignable(self) -> bool:
        """§8.2: an inactive branch cannot be assigned to new employees or job changes."""
        return self.status == "ACTIVE" and self.archived_at is None
