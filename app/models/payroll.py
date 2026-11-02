"""Payroll configuration: settings, pay groups, the period calendar and statutory registration
attributes (payroll addendum §4).

Only configuration lives here. The run, calculation and result tables arrive with the
processing engine; nothing in this module computes pay.
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
    Integer,
    SmallInteger,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.mixins import (
    CreatedColumns,
    PrimaryKeyColumn,
    StandardColumns,
    archive_reason_constraint,
)
from app.db.types import UtcDateTime, Uuid7, ciphertext
from app.db.types import percentage as percentage_column


class ProrationBasis(StrEnum):
    CALENDAR_DAYS = "CALENDAR_DAYS"
    FIXED_30 = "FIXED_30"
    WORKING_DAYS = "WORKING_DAYS"


class ComponentRounding(StrEnum):
    NEAREST_RUPEE = "NEAREST_RUPEE"
    UP_RUPEE = "UP_RUPEE"
    DOWN_RUPEE = "DOWN_RUPEE"
    TWO_DECIMALS = "TWO_DECIMALS"


class NetPayRounding(StrEnum):
    NEAREST_RUPEE = "NEAREST_RUPEE"
    NONE = "NONE"


class OneTimeTdsMethod(StrEnum):
    INCREMENTAL_IN_MONTH = "INCREMENTAL_IN_MONTH"
    SPREAD_REMAINING = "SPREAD_REMAINING"


class MaternityPayMethod(StrEnum):
    REGULAR_SALARY = "REGULAR_SALARY"
    AVERAGE_DAILY_WAGE = "AVERAGE_DAILY_WAGE"


class InputWindowMode(StrEnum):
    SAME_MONTH = "SAME_MONTH"
    OFFSET_CYCLE = "OFFSET_CYCLE"


class PayGroupStatus(StrEnum):
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"


class PeriodStatus(StrEnum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"


def _in_list(column: str, enum: type[StrEnum]) -> str:
    return f"{column} IN (" + ",".join(f"'{member.value}'" for member in enum) + ")"


class PayrollSettings(Base, StandardColumns):
    """Organization payroll settings, 1:1 with the organization (§4.2).

    Carries ``id`` and ``row_version`` like any business table so the API can use ``ETag`` /
    ``If-Match``; the 1:1 rule is the unique ``organization_id``. The three module switches of
    §4.1 stay on ``organization_settings``, where the leave and attendance switches live.
    """

    __tablename__ = "payroll_settings"
    __table_args__ = (
        UniqueConstraint("organization_id", name="uq_payroll_settings_organization"),
        UniqueConstraint("organization_id", "id", name="uq_payroll_settings_org_id"),
        CheckConstraint(
            "DAY(payroll_go_live_period) = 1", name="ck_payroll_settings_go_live_first"
        ),
        CheckConstraint(
            _in_list("default_proration_basis", ProrationBasis),
            name="ck_payroll_settings_proration_basis",
        ),
        CheckConstraint(
            _in_list("component_rounding_default", ComponentRounding),
            name="ck_payroll_settings_component_rounding",
        ),
        CheckConstraint(
            _in_list("net_pay_rounding", NetPayRounding), name="ck_payroll_settings_net_rounding"
        ),
        CheckConstraint(
            _in_list("one_time_tds_method", OneTimeTdsMethod),
            name="ck_payroll_settings_one_time_tds",
        ),
        CheckConstraint(
            _in_list("maternity_pay_method", MaternityPayMethod),
            name="ck_payroll_settings_maternity_method",
        ),
        CheckConstraint("maker_checker_mode = 'STRICT'", name="ck_payroll_settings_maker_checker"),
        CheckConstraint(
            "lop_lookback_periods BETWEEN 0 AND 12", name="ck_payroll_settings_lop_lookback"
        ),
        CheckConstraint(
            "arrear_lookback_periods BETWEEN 0 AND 36", name="ck_payroll_settings_arrear_lookback"
        ),
        CheckConstraint(
            "deduction_limit_percent BETWEEN 0 AND 100", name="ck_payroll_settings_deduction_limit"
        ),
        CheckConstraint(
            "bonus_accounting_year_start_month BETWEEN 1 AND 12",
            name="ck_payroll_settings_bonus_year_month",
        ),
        CheckConstraint(
            "alumni_access_months BETWEEN 0 AND 36", name="ck_payroll_settings_alumni_months"
        ),
        CheckConstraint(
            "fnf_wage_payment_working_days BETWEEN 0 AND 30",
            name="ck_payroll_settings_fnf_working_days",
        ),
        archive_reason_constraint("payroll_settings"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid7, ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    payroll_go_live_period: Mapped[date] = mapped_column(Date, nullable=False)
    opening_ytd_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    default_proration_basis: Mapped[str] = mapped_column(
        String(20), nullable=False, default=ProrationBasis.CALENDAR_DAYS.value
    )
    component_rounding_default: Mapped[str] = mapped_column(
        String(20), nullable=False, default=ComponentRounding.NEAREST_RUPEE.value
    )
    net_pay_rounding: Mapped[str] = mapped_column(
        String(20), nullable=False, default=NetPayRounding.NEAREST_RUPEE.value
    )
    lop_requires_finalized_attendance: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    lop_lookback_periods: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=3)
    arrear_lookback_periods: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=12)
    salary_hold_default_on_notice: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    deduction_limit_percent: Mapped[Decimal] = mapped_column(
        percentage_column(), nullable=False, default=Decimal("50.0000")
    )
    """Seed 50% until the MW rule set (§9.10) is published and can supply it."""

    maker_checker_mode: Mapped[str] = mapped_column(String(10), nullable=False, default="STRICT")
    one_time_tds_method: Mapped[str] = mapped_column(
        String(30), nullable=False, default=OneTimeTdsMethod.INCREMENTAL_IN_MONTH.value
    )
    bonus_accounting_year_start_month: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=4
    )
    gratuity_provision_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    overtime_pay_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    maternity_pay_method: Mapped[str] = mapped_column(
        String(30), nullable=False, default=MaternityPayMethod.REGULAR_SALARY.value
    )
    payslip_zero_pay_generate: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    alumni_access_months: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=18)
    fnf_wage_payment_working_days: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=2
    )


class PayGroup(Base, StandardColumns):
    """Employees processed together with one calendar (§4.3)."""

    __tablename__ = "pay_groups"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_pay_groups_org_id"),
        UniqueConstraint(
            "organization_id", "pay_group_code_normalized", name="uq_pay_groups_org_code"
        ),
        CheckConstraint(
            "pay_group_code REGEXP '^[A-Z0-9][A-Z0-9_./-]{0,29}$'",
            name="ck_pay_groups_code_pattern",
        ),
        CheckConstraint("pay_frequency = 'MONTHLY'", name="ck_pay_groups_frequency"),
        CheckConstraint(
            "proration_basis IS NULL OR proration_basis IN "
            "('CALENDAR_DAYS','FIXED_30','WORKING_DAYS')",
            name="ck_pay_groups_proration_basis",
        ),
        CheckConstraint(
            _in_list("input_window_mode", InputWindowMode), name="ck_pay_groups_window"
        ),
        CheckConstraint(
            "(input_window_mode = 'OFFSET_CYCLE' AND input_cycle_start_day BETWEEN 2 AND 28) "
            "OR (input_window_mode = 'SAME_MONTH' AND input_cycle_start_day IS NULL)",
            name="ck_pay_groups_cycle_day",
        ),
        CheckConstraint(
            "input_cutoff_offset_days BETWEEN 0 AND 15", name="ck_pay_groups_cutoff_offset"
        ),
        CheckConstraint("target_payment_day BETWEEN 1 AND 28", name="ck_pay_groups_payment_day"),
        CheckConstraint(_in_list("status", PayGroupStatus), name="ck_pay_groups_status"),
        archive_reason_constraint("pay_groups"),
        Index("ix_pay_groups_org_status", "organization_id", "status"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid7, ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    pay_group_code: Mapped[str] = mapped_column(String(30), nullable=False)
    pay_group_code_normalized: Mapped[str] = mapped_column(
        String(30), Computed("UPPER(pay_group_code)", persisted=True), nullable=False
    )
    pay_group_name: Mapped[str] = mapped_column(String(100), nullable=False)
    pay_frequency: Mapped[str] = mapped_column(String(10), nullable=False, default="MONTHLY")
    proration_basis: Mapped[str | None] = mapped_column(String(20), nullable=True)
    input_window_mode: Mapped[str] = mapped_column(
        String(20), nullable=False, default=InputWindowMode.SAME_MONTH.value
    )
    input_cycle_start_day: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    input_cutoff_offset_days: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=2)
    target_payment_day: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1)
    pay_in_period: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    status: Mapped[str] = mapped_column(
        String(10), nullable=False, default=PayGroupStatus.ACTIVE.value
    )


class PayrollPeriod(Base, StandardColumns):
    """One month of a pay group's calendar (§4.4). Never deleted."""

    __tablename__ = "payroll_periods"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_payroll_periods_org_id"),
        UniqueConstraint("pay_group_id", "period_month", name="uq_payroll_periods_group_month"),
        ForeignKeyConstraint(
            ["organization_id", "pay_group_id"],
            ["pay_groups.organization_id", "pay_groups.id"],
            name="fk_payroll_periods_pay_group",
            ondelete="RESTRICT",
        ),
        CheckConstraint("DAY(period_month) = 1", name="ck_payroll_periods_month_first"),
        CheckConstraint("period_end >= period_start", name="ck_payroll_periods_interval"),
        CheckConstraint(
            "input_window_end >= input_window_start", name="ck_payroll_periods_input_window"
        ),
        CheckConstraint(_in_list("status", PeriodStatus), name="ck_payroll_periods_status"),
        archive_reason_constraint("payroll_periods"),
        Index("ix_payroll_periods_org_month", "organization_id", "period_month"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    pay_group_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    period_month: Mapped[date] = mapped_column(Date, nullable=False)
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    tax_year_start: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    input_window_start: Mapped[date] = mapped_column(Date, nullable=False)
    input_window_end: Mapped[date] = mapped_column(Date, nullable=False)
    input_cutoff_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    payment_due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    """Null until a wage-payment rule version is published (§4.6); never a code constant."""

    target_payment_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False, default=PeriodStatus.OPEN.value)


class PfRateCategory(StrEnum):
    STANDARD = "STANDARD"
    REDUCED = "REDUCED"


class DeductorCategory(StrEnum):
    COMPANY = "COMPANY"
    GOVERNMENT = "GOVERNMENT"
    OTHERS = "OTHERS"
    INDIVIDUAL_HUF = "INDIVIDUAL_HUF"
    FIRM = "FIRM"
    LLP = "LLP"
    AOP_BOI = "AOP_BOI"
    TRUST = "TRUST"
    OTHER = "OTHER"


class StatutoryRegistrationSetting(Base, StandardColumns):
    """Payroll-only attributes of one EMS registration row (§4.5); identity stays in EMS."""

    __tablename__ = "statutory_registration_settings"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_statutory_reg_settings_org_id"),
        UniqueConstraint("registration_id", name="uq_statutory_reg_settings_registration"),
        ForeignKeyConstraint(
            ["organization_id", "registration_id"],
            ["organization_registrations.organization_id", "organization_registrations.id"],
            name="fk_statutory_reg_settings_registration",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "pf_contribution_rate_category IS NULL OR "
            + _in_list("pf_contribution_rate_category", PfRateCategory),
            name="ck_statutory_reg_settings_pf_category",
        ),
        CheckConstraint(
            "deductor_category IS NULL OR " + _in_list("deductor_category", DeductorCategory),
            name="ck_statutory_reg_settings_deductor",
        ),
        CheckConstraint(
            "(pf_exempted_trust = 1) = (pf_trust_name IS NOT NULL)",
            name="ck_statutory_reg_settings_trust_name",
        ),
        CheckConstraint(
            "(responsible_person_pan_encrypted IS NULL) = "
            "(responsible_person_pan_key_version IS NULL)",
            name="ck_statutory_reg_settings_pan_key",
        ),
        archive_reason_constraint("statutory_registration_settings"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid7, ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    registration_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    pf_contribution_rate_category: Mapped[str | None] = mapped_column(String(10), nullable=True)
    pf_exempted_trust: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    pf_trust_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    esi_implemented_area: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("1")
    )
    deductor_category: Mapped[str | None] = mapped_column(String(20), nullable=True)
    responsible_person_name: Mapped[str | None] = mapped_column(String(150), nullable=True)
    responsible_person_designation: Mapped[str | None] = mapped_column(String(100), nullable=True)
    responsible_person_pan_encrypted: Mapped[bytes | None] = mapped_column(
        ciphertext(), nullable=True
    )
    responsible_person_pan_key_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    responsible_person_pan_last_four: Mapped[str | None] = mapped_column(String(4), nullable=True)


class StatutoryRegistrationBranch(Base, PrimaryKeyColumn, CreatedColumns):
    """A branch that reports under a registration for a date range (§4.5, PCFG-10).

    Replaced wholesale with its parent setting, so it carries no update columns.
    """

    __tablename__ = "statutory_registration_branches"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_statutory_reg_branches_org_id"),
        UniqueConstraint(
            "registration_setting_id",
            "branch_id",
            "effective_from",
            name="uq_statutory_reg_branches_start",
        ),
        ForeignKeyConstraint(
            ["organization_id", "registration_setting_id"],
            [
                "statutory_registration_settings.organization_id",
                "statutory_registration_settings.id",
            ],
            name="fk_statutory_reg_branches_setting",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "branch_id"],
            ["branches.organization_id", "branches.id"],
            name="fk_statutory_reg_branches_branch",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from",
            name="ck_statutory_reg_branches_interval",
        ),
        Index("ix_statutory_reg_branches_branch", "organization_id", "branch_id"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid7, ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    registration_setting_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    branch_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    """Exclusive (§1.4)."""


__all__ = [
    "ComponentRounding",
    "DeductorCategory",
    "InputWindowMode",
    "MaternityPayMethod",
    "NetPayRounding",
    "OneTimeTdsMethod",
    "PayGroup",
    "PayGroupStatus",
    "PayrollPeriod",
    "PayrollSettings",
    "PeriodStatus",
    "PfRateCategory",
    "ProrationBasis",
    "StatutoryRegistrationBranch",
    "StatutoryRegistrationSetting",
]
