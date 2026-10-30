"""Repeated employee records: education, previous employment, family, nominees, bank accounts.

Covers §12, §13 and §14. Every table is scoped to its employee by a composite foreign key on
``(organization_id, employee_id)``, so a child row can never attach to an employee in another
tenant (§6.3, C-13).

Two tables carry sensitive data and follow the same pattern as identifiers: bank account
numbers are AES-256-GCM ciphertext plus a blind index plus a last-four fragment, and
``last_drawn_ctc`` and the background-verification fields are ``[R]`` and omitted from
responses unless the caller holds the matching permission.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Computed,
    Date,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    SmallInteger,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.mixins import StandardColumns, archive_reason_constraint
from app.db.types import Uuid7, blind_index, ciphertext, money
from app.db.types import share_percentage as percentage_column
from app.models.identifier import BLIND_INDEX_LENGTH
from app.models.org_config import NominationType


class ResultType(StrEnum):
    """§12.1 ``result_type``."""

    PERCENTAGE = "PERCENTAGE"
    CGPA = "CGPA"
    GRADE = "GRADE"
    OTHER = "OTHER"


class OverlapReviewStatus(StrEnum):
    """§12.2 ``overlap_review_status``. Overlaps are flagged for review, never rejected."""

    NOT_APPLICABLE = "NOT_APPLICABLE"
    PENDING_REVIEW = "PENDING_REVIEW"
    ACCEPTED = "ACCEPTED"


class BankVerificationStatus(StrEnum):
    """§14.1 ``verification_status``."""

    UNVERIFIED = "UNVERIFIED"
    PENDING = "PENDING"
    VERIFIED = "VERIFIED"
    FAILED = "FAILED"


def _employee_child_args(table_name: str, *extra: object) -> tuple[object, ...]:
    """Constraints shared by every per-employee child table (§6.3, C-13)."""
    return (
        UniqueConstraint("organization_id", "id", name=f"uq_{table_name}_org_id"),
        ForeignKeyConstraint(
            ["organization_id", "employee_id"],
            ["employees.organization_id", "employees.id"],
            name=f"fk_{table_name}_employee",
            ondelete="RESTRICT",
        ),
        archive_reason_constraint(table_name),
        Index(f"ix_{table_name}_employee_id", "employee_id"),
        *extra,
    )


class EmployeeEducation(Base, StandardColumns):
    """One education record (§12.1).

    §12.1 is explicit that "No employee is required to provide any particular education
    level", so nothing here is mandatory beyond the qualification level and institution.

    The result rules are the fiddly part: a CGPA is meaningless without its scale, because
    8.45 means different things out of 10 and out of 4. ``maximum_result_value`` is therefore
    required for CGPA and forbidden for grades, and §29 Q-14-style arithmetic is never applied
    to it — it is stored for display only.
    """

    __tablename__ = "employee_education"
    __table_args__ = _employee_child_args(
        "employee_education",
        # C-13 / §6.3: the referenced document must belong to the same employee.
        ForeignKeyConstraint(
            ["employee_id", "certificate_document_id"],
            ["employee_documents.employee_id", "employee_documents.id"],
            name="fk_employee_education_certificate_document",
            ondelete="RESTRICT",
            use_alter=True,
        ),
        CheckConstraint(
            "result_type IS NULL OR result_type IN ('PERCENTAGE','CGPA','GRADE','OTHER')",
            name="ck_employee_education_result_type",
        ),
        # §12.1: a degree name is required except for school-level qualifications.
        CheckConstraint(
            "qualification_level IN ('SECONDARY_10','HIGHER_SECONDARY_12') "
            "OR degree_name IS NOT NULL",
            name="ck_employee_education_degree_name_required",
        ),
        # §12.1: a passing year is required unless the course is ongoing.
        CheckConstraint(
            "is_ongoing = 1 OR passing_year IS NOT NULL",
            name="ck_employee_education_passing_year_required",
        ),
        CheckConstraint(
            "start_year IS NULL OR passing_year IS NULL OR start_year <= passing_year",
            name="ck_employee_education_year_order",
        ),
        CheckConstraint(
            "start_year IS NULL OR start_year >= 1940", name="ck_employee_education_start_year"
        ),
        CheckConstraint(
            "result_type IS NULL OR result_value IS NOT NULL",
            name="ck_employee_education_result_value_required",
        ),
        # §12.1: a maximum is mandatory for CGPA, must be null or 100 for a percentage, and
        # must be null for a grade — otherwise the stored result cannot be interpreted.
        CheckConstraint(
            "(result_type = 'CGPA' AND maximum_result_value IS NOT NULL "
            " AND maximum_result_value > 0 AND maximum_result_value <= 100) "
            "OR (result_type = 'PERCENTAGE' "
            " AND (maximum_result_value IS NULL OR maximum_result_value = 100)) "
            "OR (result_type IN ('GRADE','OTHER') AND maximum_result_value IS NULL) "
            "OR (result_type IS NULL AND maximum_result_value IS NULL)",
            name="ck_employee_education_maximum_result_value",
        ),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    employee_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)

    qualification_level: Mapped[str] = mapped_column(String(50), nullable=False)
    degree_name: Mapped[str | None] = mapped_column(String(150), nullable=True)
    specialization: Mapped[str | None] = mapped_column(String(150), nullable=True)
    institution_name: Mapped[str] = mapped_column(String(200), nullable=False)
    board_or_university: Mapped[str | None] = mapped_column(String(200), nullable=True)
    study_mode: Mapped[str | None] = mapped_column(String(50), nullable=True)
    is_ongoing: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    start_year: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    passing_year: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    result_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    result_value: Mapped[str | None] = mapped_column(String(20), nullable=True)
    """Text, not numeric: §12.1 allows a percentage, a CGPA or a letter grade in this field."""

    maximum_result_value: Mapped[Decimal | None] = mapped_column(percentage_column(), nullable=True)
    certificate_document_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    remarks: Mapped[str | None] = mapped_column(String(500), nullable=True)


class EmployeePreviousEmployment(Base, StandardColumns):
    """A prior job (§12.2).

    ``overlap_flag`` is **not stored**: §12.2 marks it derived and §23.3 defines no column, so
    it is computed on read from the employee's other intervals and joining date (DEC-014).
    Only the review decision is persisted, because that is a human judgement rather than a
    derivation.
    """

    __tablename__ = "employee_previous_employment"
    __table_args__ = _employee_child_args(
        "employee_previous_employment",
        # C-13 / §6.3: the referenced document must belong to the same employee.
        ForeignKeyConstraint(
            ["employee_id", "experience_letter_document_id"],
            ["employee_documents.employee_id", "employee_documents.id"],
            name="fk_prev_emp_experience_letter",
            ondelete="RESTRICT",
            use_alter=True,
        ),
        # C-13 / §6.3: the referenced document must belong to the same employee.
        ForeignKeyConstraint(
            ["employee_id", "relieving_letter_document_id"],
            ["employee_documents.employee_id", "employee_documents.id"],
            name="fk_prev_emp_relieving_letter",
            ondelete="RESTRICT",
            use_alter=True,
        ),
        CheckConstraint(
            "employment_end_date >= employment_start_date",
            name="ck_employee_previous_employment_date_order",
        ),
        CheckConstraint(
            "last_drawn_ctc IS NULL OR last_drawn_ctc >= 0",
            name="ck_employee_previous_employment_ctc_non_negative",
        ),
        CheckConstraint(
            "last_drawn_ctc IS NULL OR last_drawn_ctc_currency IS NOT NULL",
            name="ck_employee_previous_employment_ctc_currency",
        ),
        CheckConstraint(
            "overlap_review_status IN ('NOT_APPLICABLE','PENDING_REVIEW','ACCEPTED')",
            name="ck_employee_previous_employment_overlap_status",
        ),
        # §12.2: a concluded verification must carry its date.
        CheckConstraint(
            "background_verification_status IS NULL "
            "OR background_verification_status NOT IN ('CLEAR','DISCREPANCY','UNABLE_TO_VERIFY') "
            "OR background_verification_date IS NOT NULL",
            name="ck_employee_previous_employment_bgv_date",
        ),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    employee_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)

    employer_name: Mapped[str] = mapped_column(String(200), nullable=False)
    designation: Mapped[str | None] = mapped_column(String(150), nullable=True)
    """Free text, deliberately not a reference to the ``designations`` master: a prior
    employer's job titles are not this organization's configuration."""

    employment_start_date: Mapped[date] = mapped_column(Date, nullable=False)
    employment_end_date: Mapped[date] = mapped_column(Date, nullable=False)
    """Inclusive, like ``employees.employment_end_date`` (§1.4)."""

    last_drawn_ctc: Mapped[Decimal | None] = mapped_column(money(), nullable=True)
    """``[R]``, requires ``salary.view``. §12.2: "Stored value only; never used in
    calculations" — §4.3 forbids any arithmetic over it."""

    last_drawn_ctc_currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    reason_for_leaving: Mapped[str | None] = mapped_column(String(500), nullable=True)
    reference_name: Mapped[str | None] = mapped_column(String(150), nullable=True)
    reference_contact: Mapped[str | None] = mapped_column(String(254), nullable=True)
    """``[R]``, requires ``bgv.view``. Third-party personal data (§25.8 item 1)."""

    experience_letter_document_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    relieving_letter_document_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)

    background_verification_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    background_verification_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    overlap_review_status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=OverlapReviewStatus.NOT_APPLICABLE,
        server_default="NOT_APPLICABLE",
    )
    overlap_reviewed_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid7, ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    overlap_reviewed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    remarks: Mapped[str | None] = mapped_column(String(500), nullable=True)


class PreviousEmploymentDocumentRole(StrEnum):
    """Why a document is attached to a previous-employment record (§12.2)."""

    PAYSLIP = "PAYSLIP"


class EmployeePreviousEmploymentDocument(Base):
    """Link table for payslip documents on a previous-employment record (§12.2).

    A link table rather than a comma-separated column, which §23.1 forbids outright. The
    composite key on ``(previous_employment_id, document_id)`` makes a duplicate link
    impossible, and §22 allows these pure link rows to cascade.
    """

    __tablename__ = "employee_previous_employment_documents"
    __table_args__ = (
        CheckConstraint("document_role IN ('PAYSLIP')", name="ck_prev_emp_documents_role"),
        Index("ix_prev_emp_documents_document_id", "document_id"),
    )

    previous_employment_id: Mapped[uuid.UUID] = mapped_column(
        Uuid7,
        ForeignKey("employee_previous_employment.id", ondelete="CASCADE"),
        primary_key=True,
    )
    document_id: Mapped[uuid.UUID] = mapped_column(Uuid7, primary_key=True)
    document_role: Mapped[str] = mapped_column(
        String(20), nullable=False, default=PreviousEmploymentDocumentRole.PAYSLIP
    )


class EmployeeFamilyMember(Base, StandardColumns):
    """A family member (§13.1).

    §13.1 imposes **no restriction on the count of any relationship** — the spec is explicit
    that no assumptions about family structure are made — so there is no uniqueness constraint
    on relationship here, deliberately.

    This is third-party personal data (§25.8 item 1) and requires
    ``employee.view_personal``.
    """

    __tablename__ = "employee_family_members"
    __table_args__ = _employee_child_args(
        "employee_family_members",
        CheckConstraint(
            "date_of_birth IS NULL OR date_of_birth >= '1900-01-01'",
            name="ck_employee_family_members_dob",
        ),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    employee_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)

    full_name: Mapped[str] = mapped_column(String(150), nullable=False)
    relationship_code: Mapped[str] = mapped_column(String(50), nullable=False)
    """``LOOKUP(RELATIONSHIP)``. Named ``relationship_code`` rather than ``relationship``
    because ``relationship`` is SQLAlchemy's own ORM construct and shadowing it in a model
    class is a trap."""

    date_of_birth: Mapped[date | None] = mapped_column(Date, nullable=True)
    occupation: Mapped[str | None] = mapped_column(String(100), nullable=True)
    is_dependent: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    insurance_coverage_reference: Mapped[str | None] = mapped_column(String(100), nullable=True)
    """A text reference only — §4.2 excludes any insurance module."""

    remarks: Mapped[str | None] = mapped_column(String(500), nullable=True)


class EmployeeNominee(Base, StandardColumns):
    """A nominee within one nomination set (§13.2).

    Nominees of one type and label are maintained as a **set**, replaced atomically (§13.3):
    the current rows get ``effective_to = new effective_from`` and new rows are inserted, so
    history is retained rather than overwritten.

    ``is_minor`` is derived from the date of birth on ``effective_from`` and so is not stored;
    the guardian columns are, because they are entered data. The share total rule (C-08) spans
    rows and is validated in the service on set replacement.
    """

    __tablename__ = "employee_nominees"
    __table_args__ = _employee_child_args(
        "employee_nominees",
        # C-13 / §6.3: the referenced document must belong to the same employee.
        ForeignKeyConstraint(
            ["employee_id", "supporting_document_id"],
            ["employee_documents.employee_id", "employee_documents.id"],
            name="fk_employee_nominees_supporting_document",
            ondelete="RESTRICT",
            use_alter=True,
        ),
        CheckConstraint(
            "nomination_type IN ('PF','GRATUITY','INSURANCE','OTHER')",
            name="ck_employee_nominees_type",
        ),
        CheckConstraint(
            "share_percentage > 0 AND share_percentage <= 100",
            name="ck_employee_nominees_share_range",
        ),
        CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from",
            name="ck_employee_nominees_effective_interval",
        ),
        # §13.2: INSURANCE and OTHER need a label to tell two schemes apart.
        CheckConstraint(
            "nomination_type NOT IN ('OTHER','INSURANCE') OR nomination_label IS NOT NULL",
            name="ck_employee_nominees_label_required",
        ),
        Index(
            "ix_employee_nominees_set",
            "employee_id",
            "nomination_type",
            "effective_from",
        ),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    employee_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)

    nomination_type: Mapped[str] = mapped_column(String(20), nullable=False)
    nomination_label: Mapped[str | None] = mapped_column(String(100), nullable=True)
    family_member_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    """Optional link to a family member, so the same person is not entered twice (§13.2)."""

    nominee_name: Mapped[str] = mapped_column(String(150), nullable=False)
    relationship_code: Mapped[str] = mapped_column(String(50), nullable=False)
    date_of_birth: Mapped[date | None] = mapped_column(Date, nullable=True)
    contact_number: Mapped[str | None] = mapped_column(String(16), nullable=True)
    address: Mapped[str | None] = mapped_column(String(500), nullable=True)
    share_percentage: Mapped[Decimal] = mapped_column(percentage_column(), nullable=False)

    guardian_name: Mapped[str | None] = mapped_column(String(150), nullable=True)
    guardian_relationship: Mapped[str | None] = mapped_column(String(100), nullable=True)
    """§13.2: both are required when the nominee is a minor, which the service checks because
    minority is derived from the date of birth relative to ``effective_from``."""

    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    """Exclusive (§1.4). Set by the system when the set is replaced."""

    supporting_document_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)


class EmployeeBankAccount(Base, StandardColumns):
    """A bank account held for reference (§14.1).

    §14.2 is emphatic about the boundaries: the account number is never persisted in plaintext,
    online banking passwords, PINs, OTPs, card numbers and CVVs have **no fields and must not
    be placed in free text**, and the system never initiates a payment or generates a payment
    file. This is reference data for an HR record, nothing more.

    Changing an account number or IFSC is **not an edit**: the old row is closed with an
    ``effective_to`` and a new row created, with verification reset to ``UNVERIFIED`` (§14.2).

    **One primary account at a time (C-07).** PostgreSQL uses a GiST exclusion constraint over
    the primary interval (§23.5). MySQL cannot, so the unique key below blocks two primaries
    starting on the same date, and full interval-overlap prevention is the employee row lock
    plus an in-transaction check plus the nightly integrity job (ARCHITECTURE §7).
    """

    __tablename__ = "employee_bank_accounts"
    __table_args__ = _employee_child_args(
        "employee_bank_accounts",
        # C-13 / §6.3: the referenced document must belong to the same employee.
        ForeignKeyConstraint(
            ["employee_id", "bank_proof_document_id"],
            ["employee_documents.employee_id", "employee_documents.id"],
            name="fk_employee_bank_accounts_proof_document",
            ondelete="RESTRICT",
            use_alter=True,
        ),
        UniqueConstraint(
            "employee_id", "primary_from_key", name="uq_employee_bank_accounts_primary_start"
        ),
        CheckConstraint(
            "verification_status IN ('UNVERIFIED','PENDING','VERIFIED','FAILED')",
            name="ck_employee_bank_accounts_verification_status",
        ),
        CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from",
            name="ck_employee_bank_accounts_effective_interval",
        ),
        # §14.1: a concluded verification needs its date; a failure needs a reason.
        CheckConstraint(
            "verification_status NOT IN ('VERIFIED','FAILED') OR verification_date IS NOT NULL",
            name="ck_employee_bank_accounts_verification_date",
        ),
        CheckConstraint(
            "verification_status <> 'FAILED' OR verification_remarks IS NOT NULL",
            name="ck_employee_bank_accounts_failure_remarks",
        ),
        # §14.1: proof of the account is required before it can be marked verified.
        CheckConstraint(
            "verification_status <> 'VERIFIED' OR bank_proof_document_id IS NOT NULL",
            name="ck_employee_bank_accounts_verified_requires_proof",
        ),
        CheckConstraint(
            f"LENGTH(account_number_blind_index) = {BLIND_INDEX_LENGTH}",
            name="ck_employee_bank_accounts_blind_index_length",
        ),
        CheckConstraint("key_version >= 1", name="ck_employee_bank_accounts_key_version"),
        Index(
            "ix_employee_bank_accounts_org_blind",
            "organization_id",
            "account_number_blind_index",
        ),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    employee_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)

    account_holder_name: Mapped[str] = mapped_column(String(150), nullable=False)
    bank_name: Mapped[str] = mapped_column(String(150), nullable=False)
    """Entered manually: §29 Q-16 decides against loading an IFSC reference dataset in v1."""

    branch_name: Mapped[str | None] = mapped_column(String(150), nullable=True)

    account_number_encrypted: Mapped[bytes] = mapped_column(ciphertext(), nullable=False)
    account_number_last_four: Mapped[str] = mapped_column(String(4), nullable=False)
    account_number_blind_index: Mapped[bytes] = mapped_column(blind_index(), nullable=False)
    """HMAC over IFSC plus account number (§14.1), so the same number at two different banks
    does not read as a duplicate. Drives the ``W-BANK-DUP`` warning."""

    key_version: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=1, server_default=text("1")
    )

    ifsc_code: Mapped[str] = mapped_column(String(11), nullable=False)
    account_type: Mapped[str] = mapped_column(String(50), nullable=False)
    is_primary: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    primary_from_key: Mapped[date | None] = mapped_column(
        Date,
        Computed(
            "IF(is_primary = 1 AND archived_at IS NULL, effective_from, NULL)", persisted=True
        ),
        nullable=True,
    )

    verification_status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=BankVerificationStatus.UNVERIFIED,
        server_default="UNVERIFIED",
    )
    verification_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    verified_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid7, ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    verification_remarks: Mapped[str | None] = mapped_column(String(500), nullable=True)
    bank_proof_document_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)

    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    """Exclusive (§1.4). Set when the account is closed."""


__all__ = [
    "BankVerificationStatus",
    "EmployeeBankAccount",
    "EmployeeEducation",
    "EmployeeFamilyMember",
    "EmployeeNominee",
    "EmployeePreviousEmployment",
    "EmployeePreviousEmploymentDocument",
    "NominationType",
    "OverlapReviewStatus",
    "PreviousEmploymentDocumentRole",
    "ResultType",
]
