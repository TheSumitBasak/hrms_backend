"""Employee code generation: rules, sequence counters and the issued-code registry (§19).

Three tables with sharply different jobs:

``employee_id_generation_rules``
    Effective-dated configuration describing how a code is rendered. Immutable once it has
    issued a code or its start date has passed (§19.3), because issued codes embed its output.

``employee_id_sequences``
    The authoritative counters. §19.6 is emphatic: "The next sequence is **never** derived by
    counting employees or reading MAX(code)." Issuance locks the row with
    ``SELECT … FOR UPDATE``, which is what makes AC-16's fifty concurrent creations produce
    fifty unique, contiguous codes.

``employee_code_registry``
    Every code ever issued, including retired ones. Append-mostly: the only permitted change is
    ``ASSIGNED → RETIRED``. A retired code is reserved forever and never reissued (§19.7), so
    this table — not ``employees`` — is what the generator checks for collisions, since a code
    issued to a since-discarded draft must still not be handed out again.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
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
from app.db.mixins import AppendOnlyColumns, StandardColumns, archive_reason_constraint
from app.db.types import Uuid7

MAX_RENDERED_CODE_LENGTH = 30
"""§19.1: a rendered code is at most 30 characters and matches
``^[A-Z0-9][A-Z0-9/_-]{0,29}$``."""

SEQUENCE_COLLISION_LIMIT = 1000
"""§19.6 step 5: after this many consecutive collisions, fail with
``409 ID_SEQUENCE_COLLISION_LIMIT`` rather than looping."""

NULL_DEPARTMENT_SCOPE = "00000000000000000000000000000000"
"""Stands in for "organization-wide" in the conflict key, mirroring the sentinel UUID the
§23.5 PostgreSQL exclusion constraint uses via ``COALESCE``."""


class YearFormat(StrEnum):
    """§19.1 ``year_format``."""

    YYYY = "YYYY"
    YY = "YY"
    FY_SHORT = "FY_SHORT"
    FY_LONG = "FY_LONG"


class SequenceResetPolicy(StrEnum):
    """§19.1 ``sequence_reset_policy``."""

    NEVER = "NEVER"
    CALENDAR_YEAR = "CALENDAR_YEAR"
    FINANCIAL_YEAR = "FINANCIAL_YEAR"


class IdRuleStatus(StrEnum):
    """§19.1 ``status``. DRAFT is explicit; the middle three are derived from dates."""

    DRAFT = "DRAFT"
    SCHEDULED = "SCHEDULED"
    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    ARCHIVED = "ARCHIVED"


SELECTABLE_RULE_STATUSES: frozenset[IdRuleStatus] = frozenset(
    {IdRuleStatus.SCHEDULED, IdRuleStatus.ACTIVE, IdRuleStatus.EXPIRED}
)
"""§19.5 step 1: candidates exclude DRAFT and ARCHIVED. EXPIRED rules remain candidates when
the basis date falls inside their interval, which is how a backdated joiner gets the rule that
was in force when they joined (E-09)."""


class CodeSource(StrEnum):
    """§19.9 ``source``."""

    GENERATED = "GENERATED"
    MANUAL = "MANUAL"


class CodeRegistryStatus(StrEnum):
    """§19.9 ``status``. The only legal transition is ASSIGNED → RETIRED."""

    ASSIGNED = "ASSIGNED"
    RETIRED = "RETIRED"


class EmployeeIdGenerationRule(Base, StandardColumns):
    """An effective-dated employee code format (§19.1).

    **Conflict prevention (§19.4, C-11).** Two rules conflict when they share organization,
    department scope and priority, are neither DRAFT nor ARCHIVED, and their effective
    intervals overlap. PostgreSQL expresses that as a GiST exclusion constraint (§23.5); MySQL
    cannot. The unique constraint here catches the same-start case, and the overlapping-range
    case is checked in the service under a lock at publish time and re-checked by the nightly
    integrity job (ARCHITECTURE §7). Rules with *different* priorities may overlap by design —
    §19.5 then resolves precedence deterministically.
    """

    __tablename__ = "employee_id_generation_rules"
    __table_args__ = (
        UniqueConstraint("organization_id", "rule_code_normalized", name="uq_id_rules_org_code"),
        UniqueConstraint("organization_id", "id", name="uq_id_rules_org_id"),
        # The same-start half of C-11. The key is NULL for DRAFT and ARCHIVED rules, which are
        # excluded from conflict detection, so they never participate (ADR-003).
        UniqueConstraint("conflict_key", name="uq_id_rules_conflict_key"),
        ForeignKeyConstraint(
            ["organization_id", "department_id"],
            ["departments.organization_id", "departments.id"],
            name="fk_id_rules_department",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "rule_code REGEXP '^[A-Z0-9][A-Z0-9_./-]{0,29}$'", name="ck_id_rules_rule_code"
        ),
        CheckConstraint(
            "prefix IS NULL OR prefix REGEXP '^[A-Z0-9]{1,10}$'", name="ck_id_rules_prefix"
        ),
        CheckConstraint(
            "suffix IS NULL OR suffix REGEXP '^[A-Z0-9]{1,10}$'", name="ck_id_rules_suffix"
        ),
        # `separator` is a MySQL reserved word (GROUP_CONCAT ... SEPARATOR), so it is
        # backtick-quoted inside raw constraint SQL.
        CheckConstraint(
            "`separator` IS NULL OR `separator` IN ('-','/','_')",
            name="ck_id_rules_separator",
        ),
        # §19.1: {SEQ} appears exactly once, so a rendered code always has a unique part.
        CheckConstraint("format_pattern LIKE '%{SEQ}%'", name="ck_id_rules_pattern_requires_seq"),
        # §19.1: the separator is required when the pattern uses {SEP}.
        CheckConstraint(
            "format_pattern NOT LIKE '%{SEP}%' OR `separator` IS NOT NULL",
            name="ck_id_rules_sep_requires_separator",
        ),
        # §19.1: year_format is required if and only if the pattern contains {YEAR}.
        CheckConstraint(
            "(format_pattern LIKE '%{YEAR}%' AND year_format IS NOT NULL) "
            "OR (format_pattern NOT LIKE '%{YEAR}%' AND year_format IS NULL)",
            name="ck_id_rules_year_format_iff_year_token",
        ),
        # §19.1: a calendar-year reset needs a calendar year token and a financial-year reset
        # needs a financial one, otherwise codes would collide after the counter resets.
        CheckConstraint(
            "sequence_reset_policy = 'NEVER' "
            "OR (sequence_reset_policy = 'CALENDAR_YEAR' AND year_format IN ('YYYY','YY')) "
            "OR (sequence_reset_policy = 'FINANCIAL_YEAR' "
            "AND year_format IN ('FY_SHORT','FY_LONG'))",
            name="ck_id_rules_reset_policy_matches_year_format",
        ),
        CheckConstraint("sequence_start >= 0", name="ck_id_rules_sequence_start"),
        CheckConstraint(
            "sequence_increment BETWEEN 1 AND 100", name="ck_id_rules_sequence_increment"
        ),
        CheckConstraint(
            "sequence_padding_length BETWEEN 1 AND 10", name="ck_id_rules_padding_length"
        ),
        CheckConstraint("priority BETWEEN 1 AND 999", name="ck_id_rules_priority"),
        CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from",
            name="ck_id_rules_effective_interval",
        ),
        CheckConstraint(
            "status IN ('DRAFT','SCHEDULED','ACTIVE','EXPIRED','ARCHIVED')",
            name="ck_id_rules_status",
        ),
        CheckConstraint(
            "year_format IS NULL OR year_format IN ('YYYY','YY','FY_SHORT','FY_LONG')",
            name="ck_id_rules_year_format",
        ),
        archive_reason_constraint("employee_id_generation_rules"),
        # §19.5 selection order: department-specific first, then priority, then start date.
        Index(
            "ix_id_rules_selection",
            "organization_id",
            "department_id",
            "status",
            "effective_from",
        ),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid7, ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    department_id: Mapped[uuid.UUID | None] = mapped_column(Uuid7, nullable=True)
    """Null means organization-wide. A department-specific rule applies only to employees of
    that department (§19.5)."""

    rule_name: Mapped[str] = mapped_column(String(100), nullable=False)
    """Editable at any time — it is a label, not part of the output (§19.3)."""

    rule_code: Mapped[str] = mapped_column(String(30), nullable=False)
    rule_code_normalized: Mapped[str] = mapped_column(
        String(30), Computed("UPPER(rule_code)", persisted=True), nullable=False
    )

    prefix: Mapped[str | None] = mapped_column(String(10), nullable=True)
    suffix: Mapped[str | None] = mapped_column(String(10), nullable=True)
    format_pattern: Mapped[str] = mapped_column(String(100), nullable=False)
    """The authoritative format. Tokens: ``{ORG} {DEPT} {PREFIX} {SUFFIX} {YEAR} {SEQ}
    {SEP}``. The ``include_*`` flags of §19.1 are derived from this and are not stored, so
    they cannot drift out of step with the pattern."""

    separator: Mapped[str | None] = mapped_column(String(1), nullable=True)
    year_format: Mapped[str | None] = mapped_column(String(10), nullable=True)

    sequence_reset_policy: Mapped[str] = mapped_column(
        String(20), nullable=False, default=SequenceResetPolicy.NEVER, server_default="NEVER"
    )
    sequence_group_code: Mapped[str] = mapped_column(String(30), nullable=False)
    """Rules sharing a group share the counter, which is how numbering continues across rule
    versions (§19.1). They must agree on increment and reset policy — checked in the service,
    since the constraint spans rows."""

    sequence_start: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=1, server_default=text("1")
    )
    sequence_increment: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=1, server_default=text("1")
    )
    sequence_padding_length: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=4, server_default=text("4")
    )
    """Left zero-padding. Longer numbers are **not** truncated (§19.1); a code that would
    exceed 30 characters fails validation and the rule UI warns at 90% capacity (E-35)."""

    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    priority: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=100, server_default=text("100")
    )
    """1 is highest. Rules of differing priority may overlap (§19.4)."""

    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=IdRuleStatus.DRAFT, server_default="DRAFT"
    )
    published_at: Mapped[datetime | None] = mapped_column(nullable=True)
    published_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid7, ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )

    conflict_key: Mapped[str | None] = mapped_column(
        String(120),
        Computed(
            "IF(status IN ('DRAFT','ARCHIVED'), NULL, "
            "CONCAT(HEX(organization_id), ':', "
            f"COALESCE(HEX(department_id), '{NULL_DEPARTMENT_SCOPE}'), ':', "
            "LPAD(priority, 3, '0'), ':', effective_from))",
            persisted=True,
        ),
        nullable=True,
    )


class EmployeeIdSequence(Base):
    """A code counter for one (organization, sequence group, scope) (§19.9).

    No standard columns: this is a counter, not a business record. It is never archived and
    never deleted (§23.2), because deleting it would restart numbering and reissue codes.

    ``last_issued_value`` may legitimately be negative at seeding: §19.6 step 3 inserts the row
    at ``sequence_start - increment``, so with a start of 0 and an increment of 100 the minimum
    is -100 — which is exactly the lower bound §23.3 specifies (DEC-019).
    """

    __tablename__ = "employee_id_sequences"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "sequence_group_code",
            "scope_key",
            name="uq_employee_id_sequences_scope",
        ),
        CheckConstraint("last_issued_value >= -100", name="ck_employee_id_sequences_lower_bound"),
        CheckConstraint(
            "scope_key = 'ALL' OR scope_key REGEXP '^(CY|FY)[0-9]{4}$'",
            name="ck_employee_id_sequences_scope_key",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid7, primary_key=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid7, ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    sequence_group_code: Mapped[str] = mapped_column(String(30), nullable=False)
    scope_key: Mapped[str] = mapped_column(String(20), nullable=False)
    """``ALL``, ``CY2027`` or ``FY2026`` (§19.6 step 2)."""

    last_issued_value: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP(6)"),
        server_onupdate=text("CURRENT_TIMESTAMP(6)"),
    )


class EmployeeCodeRegistry(Base, AppendOnlyColumns):
    """Every employee code ever issued in an organization (§19.9).

    This is the uniqueness authority for codes (C-02), not ``employees``: a code issued to a
    draft that was later discarded is retired here and must never be reissued, even though no
    employee row carries it any more. The generator's collision check (§19.6 step 5) therefore
    queries this table.

    Append-mostly rather than strictly append-only — ``ASSIGNED → RETIRED`` is the single
    permitted mutation — so it uses the append-only column set plus explicit retirement
    columns rather than the full standard set.
    """

    __tablename__ = "employee_code_registry"
    __table_args__ = (
        UniqueConstraint(
            "organization_id", "employee_code_normalized", name="uq_employee_code_registry_org_code"
        ),
        UniqueConstraint("organization_id", "id", name="uq_employee_code_registry_org_id"),
        ForeignKeyConstraint(
            ["organization_id", "employee_id"],
            ["employees.organization_id", "employees.id"],
            name="fk_employee_code_registry_employee",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "source IN ('GENERATED','MANUAL')", name="ck_employee_code_registry_source"
        ),
        CheckConstraint(
            "status IN ('ASSIGNED','RETIRED')", name="ck_employee_code_registry_status"
        ),
        # §19.9: a generated code records which rule and counter produced it; a manual code
        # has no rule (§19.8) and advances no sequence.
        CheckConstraint(
            "(source = 'GENERATED' AND rule_id IS NOT NULL "
            "AND sequence_group_code IS NOT NULL AND scope_key IS NOT NULL "
            "AND sequence_value IS NOT NULL) "
            "OR (source = 'MANUAL' AND rule_id IS NULL)",
            name="ck_employee_code_registry_generated_fields",
        ),
        CheckConstraint(
            "status <> 'RETIRED' OR (retired_reason IS NOT NULL AND retired_at IS NOT NULL)",
            name="ck_employee_code_registry_retirement_fields",
        ),
        CheckConstraint(
            "employee_code_normalized REGEXP '^[A-Z0-9][A-Z0-9/_-]{0,29}$'",
            name="ck_employee_code_registry_code_pattern",
        ),
        Index("ix_employee_code_registry_employee", "organization_id", "employee_id"),
        Index("ix_employee_code_registry_status", "organization_id", "status"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid7, ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    employee_code_normalized: Mapped[str] = mapped_column(
        String(MAX_RENDERED_CODE_LENGTH), nullable=False
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(Uuid7, nullable=False)
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    rule_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid7, ForeignKey("employee_id_generation_rules.id", ondelete="RESTRICT"), nullable=True
    )
    sequence_group_code: Mapped[str | None] = mapped_column(String(30), nullable=True)
    scope_key: Mapped[str | None] = mapped_column(String(20), nullable=True)
    sequence_value: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=CodeRegistryStatus.ASSIGNED, server_default="ASSIGNED"
    )
    retired_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    issued_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=text("CURRENT_TIMESTAMP(6)")
    )
    issued_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid7, ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    retired_at: Mapped[datetime | None] = mapped_column(nullable=True)
    retired_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid7, ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
