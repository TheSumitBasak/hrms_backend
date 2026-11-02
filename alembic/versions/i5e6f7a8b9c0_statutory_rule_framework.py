"""statutory rule framework

Revision ID: i5e6f7a8b9c0
Revises: h4d5e6f7a8b9
Create Date: 2026-11-02 17:00:00.000000

Payroll addendum 4.6: platform-level rule sets, versions, parameters, slabs and list values.
Legal sources, organization overrides and the tax provision map arrive in later revisions.
MySQL DDL is not transactional: a failure part-way leaves the schema partly changed.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import app.db.types

revision: str = "i5e6f7a8b9c0"
down_revision: str | None = "h4d5e6f7a8b9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NOW = sa.text("CURRENT_TIMESTAMP(6)")
_FAMILIES = (
    "INCOME_TAX','TAX_EXEMPTIONS','PF','ESI','PT','LWF','BONUS','GRATUITY','MIN_WAGE',"
    "'WAGE_CODE','OVERTIME','LEAVE_ENCASHMENT_TAX','DUE_DATES','FORM_REFERENCES"
)


def _append_only() -> list[sa.Column]:
    return [
        sa.Column("id", app.db.types.Uuid7(length=16), nullable=False),
        sa.Column("created_at", app.db.types.UtcDateTime(), server_default=_NOW, nullable=False),
        sa.Column("created_by", app.db.types.Uuid7(length=16), nullable=True),
    ]


def _version_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["rule_version_id"],
        ["statutory_rule_versions.id"],
        name=op.f(f"fk_{table}_rule_version_id"),
        ondelete="RESTRICT",
    )


def _created_by_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["created_by"], ["users.id"], name=op.f(f"fk_{table}_created_by"), ondelete="RESTRICT"
    )


def upgrade() -> None:
    op.create_table(
        "statutory_rule_sets",
        sa.Column("rule_family", sa.String(length=30), nullable=False),
        sa.Column("jurisdiction_code", sa.String(length=10), nullable=False),
        sa.Column("rule_set_code", sa.String(length=40), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=True),
        sa.Column("status", sa.String(length=10), nullable=False),
        sa.Column("updated_at", app.db.types.UtcDateTime(), server_default=_NOW, nullable=False),
        sa.Column("updated_by", app.db.types.Uuid7(length=16), nullable=True),
        sa.Column("row_version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("archived_at", app.db.types.UtcDateTime(), nullable=True),
        sa.Column("archived_by", app.db.types.Uuid7(length=16), nullable=True),
        sa.Column("archive_reason", sa.String(length=500), nullable=True),
        *_append_only(),
        sa.CheckConstraint(f"rule_family IN ('{_FAMILIES}')", name="ck_statutory_rule_sets_family"),
        sa.CheckConstraint("status IN ('ACTIVE','INACTIVE')", name="ck_statutory_rule_sets_status"),
        sa.CheckConstraint(
            "(archived_at IS NULL AND archive_reason IS NULL) OR (archived_at IS NOT NULL AND archive_reason IS NOT NULL AND CHAR_LENGTH(archive_reason) BETWEEN 10 AND 500)",
            name="ck_statutory_rule_sets_archive_reason",
        ),
        sa.ForeignKeyConstraint(
            ["archived_by"],
            ["users.id"],
            name=op.f("fk_statutory_rule_sets_archived_by"),
            ondelete="RESTRICT",
        ),
        _created_by_fk("statutory_rule_sets"),
        sa.ForeignKeyConstraint(
            ["updated_by"],
            ["users.id"],
            name=op.f("fk_statutory_rule_sets_updated_by"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_statutory_rule_sets")),
        sa.UniqueConstraint(
            "rule_family", "jurisdiction_code", "rule_set_code", name="uq_statutory_rule_sets_code"
        ),
    )
    op.create_table(
        "statutory_rule_versions",
        sa.Column("rule_set_id", app.db.types.Uuid7(length=16), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column("status", sa.String(length=10), nullable=False),
        sa.Column("parameter_schema_version", sa.String(length=20), nullable=False),
        sa.Column("verification_status", sa.String(length=25), nullable=False),
        sa.Column("legal_review_notes", sa.Text(), nullable=True),
        sa.Column("prepared_by", app.db.types.Uuid7(length=16), nullable=False),
        sa.Column("published_by", app.db.types.Uuid7(length=16), nullable=True),
        sa.Column("published_at", app.db.types.UtcDateTime(), nullable=True),
        sa.Column("updated_at", app.db.types.UtcDateTime(), server_default=_NOW, nullable=False),
        sa.Column("updated_by", app.db.types.Uuid7(length=16), nullable=True),
        sa.Column("row_version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("archived_at", app.db.types.UtcDateTime(), nullable=True),
        sa.Column("archived_by", app.db.types.Uuid7(length=16), nullable=True),
        sa.Column("archive_reason", sa.String(length=500), nullable=True),
        *_append_only(),
        sa.CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from",
            name="ck_statutory_rule_versions_interval",
        ),
        sa.CheckConstraint(
            "status IN ('DRAFT','PUBLISHED','WITHDRAWN')",
            name="ck_statutory_rule_versions_status",
        ),
        sa.CheckConstraint(
            "verification_status IN ('UNVERIFIED','SECONDARY_CONFIRMED','PRIMARY_CONFIRMED','LEGALLY_APPROVED')",
            name="ck_statutory_rule_versions_verification",
        ),
        sa.CheckConstraint(
            "published_by IS NULL OR published_by <> prepared_by",
            name="ck_statutory_rule_versions_maker_checker",
        ),
        sa.CheckConstraint(
            "(status = 'PUBLISHED') = (published_by IS NOT NULL AND published_at IS NOT NULL) "
            "OR status = 'WITHDRAWN'",
            name="ck_statutory_rule_versions_published_audit",
        ),
        sa.CheckConstraint(
            "(archived_at IS NULL AND archive_reason IS NULL) OR (archived_at IS NOT NULL AND archive_reason IS NOT NULL AND CHAR_LENGTH(archive_reason) BETWEEN 10 AND 500)",
            name="ck_statutory_rule_versions_archive_reason",
        ),
        sa.ForeignKeyConstraint(
            ["archived_by"],
            ["users.id"],
            name=op.f("fk_statutory_rule_versions_archived_by"),
            ondelete="RESTRICT",
        ),
        _created_by_fk("statutory_rule_versions"),
        sa.ForeignKeyConstraint(
            ["prepared_by"],
            ["users.id"],
            name=op.f("fk_statutory_rule_versions_prepared_by"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["published_by"],
            ["users.id"],
            name=op.f("fk_statutory_rule_versions_published_by"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["rule_set_id"],
            ["statutory_rule_sets.id"],
            name=op.f("fk_statutory_rule_versions_rule_set_id"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by"],
            ["users.id"],
            name=op.f("fk_statutory_rule_versions_updated_by"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_statutory_rule_versions")),
        sa.UniqueConstraint(
            "rule_set_id", "version_number", name="uq_statutory_rule_versions_number"
        ),
    )
    op.create_index(
        "ix_statutory_rule_versions_effective",
        "statutory_rule_versions",
        ["rule_set_id", "effective_from"],
        unique=False,
    )
    op.create_table(
        "statutory_rule_parameters",
        sa.Column("rule_version_id", app.db.types.Uuid7(length=16), nullable=False),
        sa.Column("parameter_code", sa.String(length=60), nullable=False),
        sa.Column("value_type", sa.String(length=10), nullable=False),
        sa.Column("value_decimal", sa.Numeric(precision=19, scale=6), nullable=True),
        sa.Column("value_text", sa.String(length=500), nullable=True),
        sa.Column("value_boolean", sa.Boolean(), nullable=True),
        sa.Column("value_date", sa.Date(), nullable=True),
        *_append_only(),
        sa.CheckConstraint(
            "value_type IN ('DECIMAL','MONEY','RATE_PCT','INTEGER','BOOLEAN','DATE','TEXT','CODE')",
            name="ck_statutory_rule_parameters_type",
        ),
        sa.CheckConstraint(
            "(value_decimal IS NOT NULL) + (value_text IS NOT NULL) + "
            "(value_boolean IS NOT NULL) + (value_date IS NOT NULL) = 1",
            name="ck_statutory_rule_parameters_one_value",
        ),
        _created_by_fk("statutory_rule_parameters"),
        _version_fk("statutory_rule_parameters"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_statutory_rule_parameters")),
        sa.UniqueConstraint(
            "rule_version_id", "parameter_code", name="uq_statutory_rule_parameters_code"
        ),
    )
    op.create_table(
        "statutory_rule_slabs",
        sa.Column("rule_version_id", app.db.types.Uuid7(length=16), nullable=False),
        sa.Column("slab_table_code", sa.String(length=40), nullable=False),
        sa.Column("sequence_no", sa.SmallInteger(), nullable=False),
        sa.Column("condition_code", sa.String(length=40), nullable=False),
        sa.Column("lower_bound", sa.Numeric(precision=15, scale=2), nullable=False),
        sa.Column("upper_bound", sa.Numeric(precision=15, scale=2), nullable=True),
        sa.Column("rate_pct", sa.Numeric(precision=7, scale=4), nullable=True),
        sa.Column("fixed_amount", sa.Numeric(precision=15, scale=2), nullable=True),
        *_append_only(),
        sa.CheckConstraint(
            "rate_pct IS NOT NULL OR fixed_amount IS NOT NULL",
            name="ck_statutory_rule_slabs_amount",
        ),
        sa.CheckConstraint(
            "upper_bound IS NULL OR upper_bound > lower_bound",
            name="ck_statutory_rule_slabs_bounds",
        ),
        _created_by_fk("statutory_rule_slabs"),
        _version_fk("statutory_rule_slabs"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_statutory_rule_slabs")),
        sa.UniqueConstraint(
            "rule_version_id",
            "slab_table_code",
            "condition_code",
            "sequence_no",
            name="uq_statutory_rule_slabs_sequence",
        ),
    )
    op.create_table(
        "statutory_rule_list_values",
        sa.Column("rule_version_id", app.db.types.Uuid7(length=16), nullable=False),
        sa.Column("list_code", sa.String(length=40), nullable=False),
        sa.Column("value_code", sa.String(length=100), nullable=False),
        sa.Column("label", sa.String(length=200), nullable=False),
        *_append_only(),
        _created_by_fk("statutory_rule_list_values"),
        _version_fk("statutory_rule_list_values"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_statutory_rule_list_values")),
        sa.UniqueConstraint(
            "rule_version_id", "list_code", "value_code", name="uq_statutory_rule_list_values_code"
        ),
    )


def downgrade() -> None:
    op.drop_table("statutory_rule_list_values")
    op.drop_table("statutory_rule_slabs")
    op.drop_table("statutory_rule_parameters")
    op.drop_index("ix_statutory_rule_versions_effective", table_name="statutory_rule_versions")
    op.drop_table("statutory_rule_versions")
    op.drop_table("statutory_rule_sets")
