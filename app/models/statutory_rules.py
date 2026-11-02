"""Statutory rule framework: versioned, effective-dated law as data (payroll addendum §4.6).

Platform-level (not organization-scoped): the System Super Admin maintains one copy that every
organization resolves against. Tax slabs, PF and ESI rates, PT schedules and the like are rows
here, never constants in code.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.mixins import AppendOnlyColumns, StandardColumns, archive_reason_constraint
from app.db.types import UtcDateTime, Uuid7, money, percentage


class RuleFamily(StrEnum):
    INCOME_TAX = "INCOME_TAX"
    TAX_EXEMPTIONS = "TAX_EXEMPTIONS"
    PF = "PF"
    ESI = "ESI"
    PT = "PT"
    LWF = "LWF"
    BONUS = "BONUS"
    GRATUITY = "GRATUITY"
    MIN_WAGE = "MIN_WAGE"
    WAGE_CODE = "WAGE_CODE"
    OVERTIME = "OVERTIME"
    LEAVE_ENCASHMENT_TAX = "LEAVE_ENCASHMENT_TAX"
    DUE_DATES = "DUE_DATES"
    FORM_REFERENCES = "FORM_REFERENCES"


class RuleVersionStatus(StrEnum):
    DRAFT = "DRAFT"
    PUBLISHED = "PUBLISHED"
    WITHDRAWN = "WITHDRAWN"


class VerificationStatus(StrEnum):
    UNVERIFIED = "UNVERIFIED"
    SECONDARY_CONFIRMED = "SECONDARY_CONFIRMED"
    PRIMARY_CONFIRMED = "PRIMARY_CONFIRMED"
    LEGALLY_APPROVED = "LEGALLY_APPROVED"


class ParameterValueType(StrEnum):
    DECIMAL = "DECIMAL"
    MONEY = "MONEY"
    RATE_PCT = "RATE_PCT"
    INTEGER = "INTEGER"
    BOOLEAN = "BOOLEAN"
    DATE = "DATE"
    TEXT = "TEXT"
    CODE = "CODE"


def _in_list(column: str, enum: type[StrEnum]) -> str:
    return f"{column} IN (" + ",".join(f"'{member.value}'" for member in enum) + ")"


class StatutoryRuleSet(Base, StandardColumns):
    """One family of rules for one jurisdiction, for example PF for India (§4.6)."""

    __tablename__ = "statutory_rule_sets"
    __table_args__ = (
        UniqueConstraint(
            "rule_family", "jurisdiction_code", "rule_set_code", name="uq_statutory_rule_sets_code"
        ),
        CheckConstraint(_in_list("rule_family", RuleFamily), name="ck_statutory_rule_sets_family"),
        CheckConstraint("status IN ('ACTIVE','INACTIVE')", name="ck_statutory_rule_sets_status"),
        archive_reason_constraint("statutory_rule_sets"),
    )

    rule_family: Mapped[str] = mapped_column(String(30), nullable=False)
    jurisdiction_code: Mapped[str] = mapped_column(String(10), nullable=False)
    """``IN`` for central rules, or a GST state code such as ``24``."""

    rule_set_code: Mapped[str] = mapped_column(String(40), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="ACTIVE")


class StatutoryRuleVersion(Base, StandardColumns):
    """An effective-dated version of a rule set. PUBLISHED versions are immutable (PCFG-23)."""

    __tablename__ = "statutory_rule_versions"
    __table_args__ = (
        UniqueConstraint("rule_set_id", "version_number", name="uq_statutory_rule_versions_number"),
        CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from",
            name="ck_statutory_rule_versions_interval",
        ),
        CheckConstraint(
            _in_list("status", RuleVersionStatus), name="ck_statutory_rule_versions_status"
        ),
        CheckConstraint(
            _in_list("verification_status", VerificationStatus),
            name="ck_statutory_rule_versions_verification",
        ),
        # PCFG-23 as a database rule too, so no code path can self-publish.
        CheckConstraint(
            "published_by IS NULL OR published_by <> prepared_by",
            name="ck_statutory_rule_versions_maker_checker",
        ),
        CheckConstraint(
            "(status = 'PUBLISHED') = (published_by IS NOT NULL AND published_at IS NOT NULL) "
            "OR status = 'WITHDRAWN'",
            name="ck_statutory_rule_versions_published_audit",
        ),
        archive_reason_constraint("statutory_rule_versions"),
        Index("ix_statutory_rule_versions_effective", "rule_set_id", "effective_from"),
    )

    rule_set_id: Mapped[uuid.UUID] = mapped_column(
        Uuid7, ForeignKey("statutory_rule_sets.id", ondelete="RESTRICT"), nullable=False
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    """Exclusive (§1.4); set when the next version is published (PCFG-21)."""

    status: Mapped[str] = mapped_column(
        String(10), nullable=False, default=RuleVersionStatus.DRAFT.value
    )
    parameter_schema_version: Mapped[str] = mapped_column(String(20), nullable=False)
    verification_status: Mapped[str] = mapped_column(
        String(25), nullable=False, default=VerificationStatus.UNVERIFIED.value
    )
    legal_review_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    prepared_by: Mapped[uuid.UUID] = mapped_column(
        Uuid7, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    published_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid7, ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    published_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)


class StatutoryRuleParameter(Base, AppendOnlyColumns):
    __tablename__ = "statutory_rule_parameters"
    __table_args__ = (
        UniqueConstraint(
            "rule_version_id", "parameter_code", name="uq_statutory_rule_parameters_code"
        ),
        CheckConstraint(
            _in_list("value_type", ParameterValueType), name="ck_statutory_rule_parameters_type"
        ),
        CheckConstraint(
            "(value_decimal IS NOT NULL) + (value_text IS NOT NULL) + "
            "(value_boolean IS NOT NULL) + (value_date IS NOT NULL) = 1",
            name="ck_statutory_rule_parameters_one_value",
        ),
    )

    rule_version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid7, ForeignKey("statutory_rule_versions.id", ondelete="RESTRICT"), nullable=False
    )
    parameter_code: Mapped[str] = mapped_column(String(60), nullable=False)
    value_type: Mapped[str] = mapped_column(String(10), nullable=False)
    value_decimal: Mapped[Decimal | None] = mapped_column(Numeric(19, 6), nullable=True)
    value_text: Mapped[str | None] = mapped_column(String(500), nullable=True)
    value_boolean: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    value_date: Mapped[date | None] = mapped_column(Date, nullable=True)


class StatutoryRuleSlab(Base, AppendOnlyColumns):
    """One band of a slab table (tax rates, PT bands, surcharge bands)."""

    __tablename__ = "statutory_rule_slabs"
    __table_args__ = (
        UniqueConstraint(
            "rule_version_id",
            "slab_table_code",
            "condition_code",
            "sequence_no",
            name="uq_statutory_rule_slabs_sequence",
        ),
        CheckConstraint(
            "rate_pct IS NOT NULL OR fixed_amount IS NOT NULL",
            name="ck_statutory_rule_slabs_amount",
        ),
        CheckConstraint(
            "upper_bound IS NULL OR upper_bound > lower_bound",
            name="ck_statutory_rule_slabs_bounds",
        ),
    )

    rule_version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid7, ForeignKey("statutory_rule_versions.id", ondelete="RESTRICT"), nullable=False
    )
    slab_table_code: Mapped[str] = mapped_column(String(40), nullable=False)
    sequence_no: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    condition_code: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    """Empty string means unconditional; not NULL, so the unique key still bites."""

    lower_bound: Mapped[Decimal] = mapped_column(money(), nullable=False)
    """Inclusive."""

    upper_bound: Mapped[Decimal | None] = mapped_column(money(), nullable=True)
    """Exclusive; null means no upper bound."""

    rate_pct: Mapped[Decimal | None] = mapped_column(percentage(), nullable=True)
    fixed_amount: Mapped[Decimal | None] = mapped_column(money(), nullable=True)


class StatutoryRuleListValue(Base, AppendOnlyColumns):
    __tablename__ = "statutory_rule_list_values"
    __table_args__ = (
        UniqueConstraint(
            "rule_version_id", "list_code", "value_code", name="uq_statutory_rule_list_values_code"
        ),
    )

    rule_version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid7, ForeignKey("statutory_rule_versions.id", ondelete="RESTRICT"), nullable=False
    )
    list_code: Mapped[str] = mapped_column(String(40), nullable=False)
    value_code: Mapped[str] = mapped_column(String(100), nullable=False)
    label: Mapped[str] = mapped_column(String(200), nullable=False)


__all__ = [
    "ParameterValueType",
    "RuleFamily",
    "RuleVersionStatus",
    "StatutoryRuleListValue",
    "StatutoryRuleParameter",
    "StatutoryRuleSet",
    "StatutoryRuleSlab",
    "StatutoryRuleVersion",
    "VerificationStatus",
]
