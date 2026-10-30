"""Employment records, addresses, job history and status history (§10).

One ``employees`` row is **one person's employment with one organization over one continuous
engagement** (§10.1). Rehire and inter-organization transfer create a *new* row linked by
``previous_employment_record_id``; the previous row is never modified except for its own
separation fields.

This module is where the §6.3 organization-consistency rules become database constraints.
Every master reference is a **composite** foreign key on ``(organization_id, x_id)``, so
assigning a department from another tenant is rejected by the database itself, not only by the
service — which is what AC-03 verifies with a direct insert.
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
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.mixins import AppendOnlyColumns, StandardColumns, archive_reason_constraint
from app.db.types import Uuid7, share_percentage


class EmployeeStatus(StrEnum):
    """§10.2 built-in statuses. Organizations may add custom ones (§10.2, §24.4.2)."""

    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    ON_NOTICE = "ON_NOTICE"
    INACTIVE = "INACTIVE"
    RESIGNED = "RESIGNED"
    TERMINATED = "TERMINATED"
    RETIRED = "RETIRED"
    ABSCONDING = "ABSCONDING"


SEPARATED_STATUSES: frozenset[EmployeeStatus] = frozenset(
    {EmployeeStatus.RESIGNED, EmployeeStatus.TERMINATED, EmployeeStatus.RETIRED}
)
"""§10.2: employment has ended. Settable only when ``employment_end_date <= today``; use
``ON_NOTICE`` before that, which is how the spec avoids future-dated status scheduling."""

_SEPARATED_SQL = ",".join(f"'{status.value}'" for status in sorted(SEPARATED_STATUSES))


class WorkMode(StrEnum):
    """§10.6 ``work_mode``."""

    ON_SITE = "ON_SITE"
    HYBRID = "HYBRID"
    REMOTE = "REMOTE"
    OTHER = "OTHER"


class EmploymentTransitionType(StrEnum):
    """§10.4 ``employment_transition_type``, driving the §10.9 linkage rules."""

    NEW_HIRE = "NEW_HIRE"
    REHIRE = "REHIRE"
    INTER_ORG_TRANSFER = "INTER_ORG_TRANSFER"


class AgeExceptionStatus(StrEnum):
    """§10.4. Set when age at joining is below the configured minimum."""

    NONE = "NONE"
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class DisabilityStatus(StrEnum):
    """§10.4. Collected only when ``collect_disability_info`` is enabled."""

    YES = "YES"
    NO = "NO"
    UNDISCLOSED = "UNDISCLOSED"


class AddressType(StrEnum):
    """§10.5 ``employee_addresses.address_type``."""

    CURRENT = "CURRENT"
    PERMANENT = "PERMANENT"


def _enum_check(column: str, enum: type[StrEnum], name: str) -> CheckConstraint:
    values = ",".join(f"'{member.value}'" for member in enum)
    return CheckConstraint(f"{column} IN ({values})", name=name)


class Employee(Base, StandardColumns):
    """One employment record (§10.4, §10.5, §10.6, §11.4).

    Mandatory-at-activation (``M-A``) fields are **nullable in the schema** and enforced by the
    activation service, because §10.2 permits a DRAFT to exist with only ``organization_id``
    and ``first_name`` (plus department and joining date when codes are assigned on create).
    Making them NOT NULL would make a draft impossible to save, so the lifecycle stage, not the
    column, carries the requirement — and the activation check is correspondingly a service
    responsibility that must be tested directly.
    """

    __tablename__ = "employees"
    __table_args__ = (
        # §23.3 / C-02: unique per organization across *all* records including archived ones.
        # UPPER(NULL) is NULL and NULLs do not collide, so a DRAFT without a code is allowed
        # while every issued code is unique (ADR-003).
        UniqueConstraint(
            "organization_id", "employee_code_normalized", name="uq_employees_org_code"
        ),
        UniqueConstraint("organization_id", "id", name="uq_employees_org_id"),
        # §10.5 / §23.4: official email is unique only among non-separated, non-archived
        # records, so an address may be reused after someone leaves. The generated key is NULL
        # for those records. This covers the ORGANIZATION scope and the built-in separated
        # statuses; GROUP/NONE scopes and custom separated statuses are enforced in the
        # service (DEC-045).
        UniqueConstraint(
            "organization_id", "official_email_unique_key", name="uq_employees_org_official_email"
        ),
        # ---- §6.3 composite foreign keys: every reference stays inside the tenant ----
        ForeignKeyConstraint(
            ["organization_id", "department_id"],
            ["departments.organization_id", "departments.id"],
            name="fk_employees_department",
            ondelete="RESTRICT",
        ),
        # §6.3: the sub-department must belong to the employee's department, not merely to the
        # same organization. Constraining the pair is what makes that true in the database.
        ForeignKeyConstraint(
            ["department_id", "sub_department_id"],
            ["sub_departments.department_id", "sub_departments.id"],
            name="fk_employees_sub_department",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "designation_id"],
            ["designations.organization_id", "designations.id"],
            name="fk_employees_designation",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "grade_id"],
            ["grades.organization_id", "grades.id"],
            name="fk_employees_grade",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "business_unit_id"],
            ["business_units.organization_id", "business_units.id"],
            name="fk_employees_business_unit",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "cost_center_id"],
            ["cost_centers.organization_id", "cost_centers.id"],
            name="fk_employees_cost_center",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "employment_type_id"],
            ["employment_types.organization_id", "employment_types.id"],
            name="fk_employees_employment_type",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "branch_id"],
            ["branches.organization_id", "branches.id"],
            name="fk_employees_branch",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "work_location_id"],
            ["work_locations.organization_id", "work_locations.id"],
            name="fk_employees_work_location",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "employee_category_id"],
            ["employee_categories.organization_id", "employee_categories.id"],
            name="fk_employees_employee_category",
            ondelete="RESTRICT",
        ),
        # §6.3 / C-04: manager, HR contact and referrer must be in the same organization.
        ForeignKeyConstraint(
            ["organization_id", "reporting_manager_employee_id"],
            ["employees.organization_id", "employees.id"],
            name="fk_employees_reporting_manager",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "hr_contact_employee_id"],
            ["employees.organization_id", "employees.id"],
            name="fk_employees_hr_contact",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "referred_by_employee_id"],
            ["employees.organization_id", "employees.id"],
            name="fk_employees_referred_by",
            ondelete="RESTRICT",
        ),
        # ---- §10.7 cross-field constraints enforceable declaratively ----
        CheckConstraint(  # EMP-01
            "employment_end_date IS NULL OR employment_start_date IS NULL "
            "OR employment_end_date >= employment_start_date",
            name="ck_employees_emp01_end_after_start",
        ),
        CheckConstraint(  # EMP-02
            "confirmation_date IS NULL OR joining_date IS NULL "
            "OR confirmation_date >= joining_date",
            name="ck_employees_emp02_confirmation_after_joining",
        ),
        CheckConstraint(  # EMP-03
            "probation_end_date IS NULL OR joining_date IS NULL "
            "OR probation_end_date >= joining_date",
            name="ck_employees_emp03_probation_after_joining",
        ),
        CheckConstraint(  # EMP-05
            "reporting_manager_employee_id IS NULL OR reporting_manager_employee_id <> id",
            name="ck_employees_emp05_manager_not_self",
        ),
        CheckConstraint(
            "hr_contact_employee_id IS NULL OR hr_contact_employee_id <> id",
            name="ck_employees_emp05_hr_contact_not_self",
        ),
        CheckConstraint(
            "referred_by_employee_id IS NULL OR referred_by_employee_id <> id",
            name="ck_employees_emp05_referrer_not_self",
        ),
        CheckConstraint(
            "date_of_birth IS NULL OR date_of_birth >= '1900-01-01'",
            name="ck_employees_dob_not_before_1900",
        ),
        CheckConstraint(
            "disability_percentage IS NULL OR disability_percentage BETWEEN 0 AND 100",
            name="ck_employees_disability_percentage_range",
        ),
        # §10.4: disability detail is only meaningful when the status is YES.
        CheckConstraint(
            "disability_status = 'YES' OR "
            "(disability_type IS NULL AND disability_percentage IS NULL)",
            name="ck_employees_disability_detail_requires_yes",
        ),
        CheckConstraint(
            "probation_duration_months IS NULL OR probation_duration_months BETWEEN 0 AND 24",
            name="ck_employees_probation_months_range",
        ),
        CheckConstraint(
            "notice_period_days IS NULL OR notice_period_days BETWEEN 0 AND 365",
            name="ck_employees_notice_period_range",
        ),
        CheckConstraint(
            "alternate_mobile IS NULL OR personal_mobile IS NULL "
            "OR alternate_mobile <> personal_mobile",
            name="ck_employees_alternate_mobile_differs",
        ),
        # §10.2: a separated status requires an end date; ON_NOTICE does too.
        CheckConstraint(
            f"status NOT IN ({_SEPARATED_SQL},'ON_NOTICE') OR employment_end_date IS NOT NULL",
            name="ck_employees_separation_requires_end_date",
        ),
        # §10.4: the status is a built-in or an organization-defined code (§10.2).
        CheckConstraint("status REGEXP '^[A-Z_]{2,30}$'", name="ck_employees_status_pattern"),
        _enum_check("work_mode", WorkMode, "ck_employees_work_mode"),
        _enum_check(
            "employment_transition_type",
            EmploymentTransitionType,
            "ck_employees_transition_type",
        ),
        _enum_check(
            "age_exception_status", AgeExceptionStatus, "ck_employees_age_exception_status"
        ),
        CheckConstraint(
            "disability_status IS NULL OR disability_status IN ('YES','NO','UNDISCLOSED')",
            name="ck_employees_disability_status",
        ),
        # §10.9: a rehire or transfer must name the record it continues from.
        CheckConstraint(
            "employment_transition_type = 'NEW_HIRE' OR previous_employment_record_id IS NOT NULL",
            name="ck_employees_transition_requires_previous_record",
        ),
        # C-13 / §6.3: the referenced document must belong to the same employee.
        ForeignKeyConstraint(
            ["id", "photograph_document_id"],
            ["employee_documents.employee_id", "employee_documents.id"],
            name="fk_employees_photograph_document",
            ondelete="RESTRICT",
            use_alter=True,
        ),
        archive_reason_constraint("employees"),
        # §23.3 indexes: all tenant-leading, because every list and search is org-scoped.
        Index("ix_employees_org_status", "organization_id", "status"),
        Index("ix_employees_org_department", "organization_id", "department_id"),
        Index("ix_employees_org_designation", "organization_id", "designation_id"),
        Index("ix_employees_org_branch", "organization_id", "branch_id"),
        Index("ix_employees_org_joining_date", "organization_id", "joining_date"),
        Index("ix_employees_org_manager", "organization_id", "reporting_manager_employee_id"),
        Index("ix_employees_org_employment_type", "organization_id", "employment_type_id"),
        Index("ix_employees_org_email", "organization_id", "official_email"),
        # §26.2 name search. MySQL has no pg_trgm, so a prefix index serves prefix matching and
        # a FULLTEXT index serves word matching (ARCHITECTURE §7).
        Index("ix_employees_org_name_prefix", "organization_id", "full_name_normalized"),
        Index(
            "ft_employees_full_name_normalized",
            "full_name_normalized",
            mysql_prefix="FULLTEXT",
        ),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid7, ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    """C-01: immutable. There is no service path that updates it."""

    employee_code: Mapped[str | None] = mapped_column(String(30), nullable=True)
    """Null only while DRAFT with ``employee_code_assignment_point = ON_ACTIVATION`` (§10.4).
    Immutable except through explicit reassignment (§19.8)."""

    employee_code_normalized: Mapped[str | None] = mapped_column(
        String(30), Computed("UPPER(employee_code)", persisted=True), nullable=True
    )

    # ---------------------------------------------------------------- names (§10.4)
    first_name: Mapped[str] = mapped_column(String(100), nullable=False)
    middle_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    last_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    """Optional: §10.4 notes many Indian names have no surname."""

    display_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    full_name_normalized: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    """§10.4: lower-cased, NFC-normalised, whitespace-collapsed. Search only, never displayed."""

    father_name: Mapped[str | None] = mapped_column(String(150), nullable=True)
    mother_name: Mapped[str | None] = mapped_column(String(150), nullable=True)
    spouse_name: Mapped[str | None] = mapped_column(String(150), nullable=True)

    # ---------------------------------------------------------------- personal (§10.4)
    date_of_birth: Mapped[date | None] = mapped_column(Date, nullable=True)
    age_exception_status: Mapped[str] = mapped_column(
        String(10), nullable=False, default=AgeExceptionStatus.NONE, server_default="NONE"
    )
    age_exception_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    age_exception_decided_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid7, ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    """§10.4: must differ from the requester — maker-checker, enforced in the service."""

    age_exception_decided_at: Mapped[datetime | None] = mapped_column(nullable=True)

    gender: Mapped[str | None] = mapped_column(String(50), nullable=True)
    marital_status: Mapped[str | None] = mapped_column(String(50), nullable=True)
    nationality: Mapped[str | None] = mapped_column(String(2), nullable=True)
    blood_group: Mapped[str | None] = mapped_column(String(50), nullable=True)
    photograph_document_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)

    # Restricted personal data (§10.4 ``[R]``). Accepted only when the corresponding
    # organization setting is enabled, otherwise rejected with 422 FIELD_NOT_COLLECTED (§25.2).
    religion: Mapped[str | None] = mapped_column(String(50), nullable=True)
    social_category: Mapped[str | None] = mapped_column(String(50), nullable=True)
    disability_status: Mapped[str | None] = mapped_column(String(15), nullable=True)
    disability_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    disability_percentage: Mapped[Decimal | None] = mapped_column(share_percentage(), nullable=True)

    employment_transition_type: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=EmploymentTransitionType.NEW_HIRE,
        server_default="NEW_HIRE",
    )

    # ---------------------------------------------------------------- contact (§10.5)
    personal_mobile: Mapped[str | None] = mapped_column(String(16), nullable=True)
    alternate_mobile: Mapped[str | None] = mapped_column(String(16), nullable=True)
    personal_email: Mapped[str | None] = mapped_column(String(254), nullable=True)
    official_email: Mapped[str | None] = mapped_column(String(254), nullable=True)
    official_email_unique_key: Mapped[str | None] = mapped_column(
        String(254),
        Computed(
            "IF(archived_at IS NULL "
            f"AND status NOT IN ({_SEPARATED_SQL}) "
            "AND official_email IS NOT NULL, LOWER(official_email), NULL)",
            persisted=True,
        ),
        nullable=True,
    )
    permanent_address_same_as_current: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    """§10.5: when true, **no PERMANENT address row exists** and the API returns the CURRENT
    values marked derived. Supplying permanent fields alongside this flag is 422
    CONFLICTING_PERMANENT_ADDRESS."""

    emergency_contact_name: Mapped[str | None] = mapped_column(String(150), nullable=True)
    emergency_contact_relationship: Mapped[str | None] = mapped_column(String(50), nullable=True)
    emergency_contact_phone: Mapped[str | None] = mapped_column(String(16), nullable=True)
    emergency_contact_alternate_phone: Mapped[str | None] = mapped_column(String(16), nullable=True)

    # ---------------------------------------------------------------- employment (§10.6)
    joining_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    employment_type_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    employment_start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    """Q-03 default: must equal ``joining_date``. Validated in the service so a future policy
    change is one predicate, not a schema migration."""

    employment_end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    """The **inclusive** last day of employment (§1.4 exception). Wherever it bounds an
    interval the exclusive boundary is this date plus one day."""

    probation_duration_months: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    probation_end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    confirmation_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    designation_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    department_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    sub_department_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    grade_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    business_unit_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    cost_center_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    branch_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    work_location_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    employee_category_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)

    work_mode: Mapped[str] = mapped_column(
        String(20), nullable=False, default=WorkMode.ON_SITE, server_default="ON_SITE"
    )
    reporting_manager_employee_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    hr_contact_employee_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    notice_period_days: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    source_of_hire: Mapped[str | None] = mapped_column(String(50), nullable=True)
    referred_by_employee_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    offer_letter_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    appointment_letter_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    status: Mapped[str] = mapped_column(
        String(30), nullable=False, default=EmployeeStatus.DRAFT, server_default="DRAFT"
    )
    """Changed only through the status endpoint, per the §10.3 transition matrix."""

    status_effective_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    status_reason: Mapped[str | None] = mapped_column(String(50), nullable=True)

    previous_employment_record_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid7, ForeignKey("employees.id", ondelete="RESTRICT"), nullable=True
    )
    """§6.4: the **only** employee reference permitted to cross organizations, for an
    inter-organization transfer. A plain foreign key, deliberately not a composite one."""

    remarks: Mapped[str | None] = mapped_column(Text, nullable=True)
    """``[R]``: requires ``employee.remarks`` to view or edit."""

    # ---------------------------------------------------------------- statutory (§11.4)
    international_worker_status: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    """Reference only. When true, a passport becomes mandatory for activation (§11.4)."""

    professional_tax_state: Mapped[str | None] = mapped_column(String(2), nullable=True)
    labour_welfare_fund_state: Mapped[str | None] = mapped_column(String(2), nullable=True)
    """Both are recorded facts. §4.2 forbids any PT or LWF computation."""

    # ---------------------------------------------------------------- privacy (§25.6)
    legal_hold: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    """§25.6: blocks retention purge and erasure for this record. HRA only."""

    @property
    def is_separated(self) -> bool:
        """Whether employment has ended under a built-in status.

        Custom separated statuses resolve through ``employee_custom_statuses.base_behaviour``,
        which needs a query, so that check lives in the service rather than here.
        """
        return self.status in {status.value for status in SEPARATED_STATUSES}

    @property
    def is_draft(self) -> bool:
        return self.status == EmployeeStatus.DRAFT


class EmployeeAddress(Base, StandardColumns):
    """A current or permanent address (§10.5).

    At most one row per employee and type. When ``permanent_address_same_as_current`` is set on
    the employee, the PERMANENT row is **deleted** in the same transaction and the API derives
    those values from CURRENT — so there is never a stale permanent address disagreeing with
    the flag.
    """

    __tablename__ = "employee_addresses"
    __table_args__ = (
        UniqueConstraint("employee_id", "address_type", name="uq_employee_addresses_employee_type"),
        UniqueConstraint("organization_id", "id", name="uq_employee_addresses_org_id"),
        ForeignKeyConstraint(
            ["organization_id", "employee_id"],
            ["employees.organization_id", "employees.id"],
            name="fk_employee_addresses_employee",
            ondelete="RESTRICT",
        ),
        _enum_check("address_type", AddressType, "ck_employee_addresses_type"),
        CheckConstraint(
            "country <> 'IN' OR state_code IS NOT NULL",
            name="ck_employee_addresses_state_code_for_india",
        ),
        archive_reason_constraint("employee_addresses"),
        Index("ix_employee_addresses_employee_id", "employee_id"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    employee_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    address_type: Mapped[str] = mapped_column(String(10), nullable=False)

    line_1: Mapped[str] = mapped_column(String(200), nullable=False)
    line_2: Mapped[str | None] = mapped_column(String(200), nullable=True)
    city: Mapped[str] = mapped_column(String(100), nullable=False)
    district: Mapped[str | None] = mapped_column(String(100), nullable=True)
    state: Mapped[str] = mapped_column(String(100), nullable=False)
    state_code: Mapped[str | None] = mapped_column(String(2), nullable=True)
    country: Mapped[str] = mapped_column(String(2), nullable=False, default="IN")
    postal_code: Mapped[str] = mapped_column(String(10), nullable=False)


class EmployeeJobHistory(Base, AppendOnlyColumns):
    """An append-only snapshot of job attributes after a change (§10.8).

    Every field is a historical snapshot (``[H]``): the row records what the job *was*, so a
    later rename of a department or designation cannot rewrite it. Current values also live on
    ``employees`` for querying, and both are written in the same transaction (§10.1).

    ``sequence_no`` orders multiple changes on the same date, which the spec allows.
    """

    __tablename__ = "employee_job_history"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_employee_job_history_org_id"),
        UniqueConstraint(
            "employee_id",
            "effective_date",
            "sequence_no",
            name="uq_employee_job_history_employee_date_seq",
        ),
        ForeignKeyConstraint(
            ["organization_id", "employee_id"],
            ["employees.organization_id", "employees.id"],
            name="fk_employee_job_history_employee",
            ondelete="RESTRICT",
        ),
        CheckConstraint("sequence_no >= 1", name="ck_employee_job_history_sequence_no"),
        # §10.8: a CORRECTION must say why.
        CheckConstraint(
            "change_type <> 'CORRECTION' OR (reason IS NOT NULL AND CHAR_LENGTH(reason) >= 10)",
            name="ck_employee_job_history_correction_reason",
        ),
        Index(
            "ix_employee_job_history_employee_effective",
            "employee_id",
            "effective_date",
            "sequence_no",
        ),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    employee_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    effective_date: Mapped[date] = mapped_column(Date, nullable=False)
    sequence_no: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1)
    change_type: Mapped[str] = mapped_column(String(50), nullable=False)
    """``LOOKUP(JOB_CHANGE_TYPE)`` — JOINING, TRANSFER, PROMOTION, CORRECTION, ..."""

    # Full snapshot after the change (§10.8). No composite FKs: these are historical values
    # and must survive a master being deactivated, which §9.4 explicitly requires.
    department_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    sub_department_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    designation_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    grade_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    business_unit_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    cost_center_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    branch_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    work_location_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    employment_type_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    employee_category_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    work_mode: Mapped[str | None] = mapped_column(String(20), nullable=True)
    reporting_manager_employee_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    hr_contact_employee_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)

    position_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    """JA-SPEC-001 §10.9 (ADDITIVE): the position this employee held after the change.

    A snapshot with **no** foreign key, deliberately. History must stay readable after a
    position is archived, and this table already holds master ids without composite FKs for
    the same reason. It is a reporting convenience, not the occupancy source of truth —
    ``employee_position_assignments`` is (JA §2.2)."""

    reason: Mapped[str | None] = mapped_column(String(500), nullable=True)


class EmployeeStatusHistory(Base, AppendOnlyColumns):
    """An append-only record of every status change (§10.8)."""

    __tablename__ = "employee_status_history"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_employee_status_history_org_id"),
        ForeignKeyConstraint(
            ["organization_id", "employee_id"],
            ["employees.organization_id", "employees.id"],
            name="fk_employee_status_history_employee",
            ondelete="RESTRICT",
        ),
        # §10.3: a correction requires remarks of at least twenty characters.
        CheckConstraint(
            "is_correction = 0 OR (remarks IS NOT NULL AND CHAR_LENGTH(remarks) >= 20)",
            name="ck_employee_status_history_correction_remarks",
        ),
        Index(
            "ix_employee_status_history_employee_date",
            "employee_id",
            "status_effective_date",
        ),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    employee_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    from_status: Mapped[str] = mapped_column(String(30), nullable=False)
    to_status: Mapped[str] = mapped_column(String(30), nullable=False)
    status_effective_date: Mapped[date] = mapped_column(Date, nullable=False)
    employment_end_date_snapshot: Mapped[date | None] = mapped_column(Date, nullable=True)
    status_reason: Mapped[str | None] = mapped_column(String(50), nullable=True)
    remarks: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    is_correction: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
