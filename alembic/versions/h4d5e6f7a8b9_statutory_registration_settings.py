"""statutory registration settings

Revision ID: h4d5e6f7a8b9
Revises: g3c4d5e6f7a8
Create Date: 2026-11-02 15:00:00.000000

Payroll addendum 4.5: payroll-only attributes of an EMS registration row, and the branches
that report under it. MySQL DDL is not transactional: a failure part-way leaves the schema
partly changed.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import app.db.types

revision: str = "h4d5e6f7a8b9"
down_revision: str | None = "g3c4d5e6f7a8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NOW = sa.text("CURRENT_TIMESTAMP(6)")


def upgrade() -> None:
    op.create_table(
        "statutory_registration_settings",
        sa.Column("organization_id", app.db.types.Uuid7(length=16), nullable=False),
        sa.Column("registration_id", app.db.types.Uuid7(length=16), nullable=False),
        sa.Column("pf_contribution_rate_category", sa.String(length=10), nullable=True),
        sa.Column("pf_exempted_trust", sa.Boolean(), server_default=sa.text("0"), nullable=False),
        sa.Column("pf_trust_name", sa.String(length=200), nullable=True),
        sa.Column(
            "esi_implemented_area", sa.Boolean(), server_default=sa.text("1"), nullable=False
        ),
        sa.Column("deductor_category", sa.String(length=20), nullable=True),
        sa.Column("responsible_person_name", sa.String(length=150), nullable=True),
        sa.Column("responsible_person_designation", sa.String(length=100), nullable=True),
        sa.Column("responsible_person_pan_encrypted", sa.VARBINARY(length=512), nullable=True),
        sa.Column("responsible_person_pan_key_version", sa.Integer(), nullable=True),
        sa.Column("responsible_person_pan_last_four", sa.String(length=4), nullable=True),
        sa.Column("updated_at", app.db.types.UtcDateTime(), server_default=_NOW, nullable=False),
        sa.Column("updated_by", app.db.types.Uuid7(length=16), nullable=True),
        sa.Column("row_version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("archived_at", app.db.types.UtcDateTime(), nullable=True),
        sa.Column("archived_by", app.db.types.Uuid7(length=16), nullable=True),
        sa.Column("archive_reason", sa.String(length=500), nullable=True),
        sa.Column("id", app.db.types.Uuid7(length=16), nullable=False),
        sa.Column("created_at", app.db.types.UtcDateTime(), server_default=_NOW, nullable=False),
        sa.Column("created_by", app.db.types.Uuid7(length=16), nullable=True),
        sa.CheckConstraint(
            "pf_contribution_rate_category IS NULL OR "
            "pf_contribution_rate_category IN ('STANDARD','REDUCED')",
            name="ck_statutory_reg_settings_pf_category",
        ),
        sa.CheckConstraint(
            "deductor_category IS NULL OR deductor_category IN ('COMPANY','GOVERNMENT','OTHERS',"
            "'INDIVIDUAL_HUF','FIRM','LLP','AOP_BOI','TRUST','OTHER')",
            name="ck_statutory_reg_settings_deductor",
        ),
        sa.CheckConstraint(
            "(pf_exempted_trust = 1) = (pf_trust_name IS NOT NULL)",
            name="ck_statutory_reg_settings_trust_name",
        ),
        sa.CheckConstraint(
            "(responsible_person_pan_encrypted IS NULL) = "
            "(responsible_person_pan_key_version IS NULL)",
            name="ck_statutory_reg_settings_pan_key",
        ),
        sa.CheckConstraint(
            "(archived_at IS NULL AND archive_reason IS NULL) OR (archived_at IS NOT NULL AND archive_reason IS NOT NULL AND CHAR_LENGTH(archive_reason) BETWEEN 10 AND 500)",
            name="ck_statutory_registration_settings_archive_reason",
        ),
        sa.ForeignKeyConstraint(
            ["archived_by"],
            ["users.id"],
            name=op.f("fk_statutory_registration_settings_archived_by"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_statutory_registration_settings_created_by"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_statutory_registration_settings_organization_id"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by"],
            ["users.id"],
            name=op.f("fk_statutory_registration_settings_updated_by"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "registration_id"],
            ["organization_registrations.organization_id", "organization_registrations.id"],
            name="fk_statutory_reg_settings_registration",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_statutory_registration_settings")),
        sa.UniqueConstraint("organization_id", "id", name="uq_statutory_reg_settings_org_id"),
        sa.UniqueConstraint("registration_id", name="uq_statutory_reg_settings_registration"),
    )
    op.create_table(
        "statutory_registration_branches",
        sa.Column("organization_id", app.db.types.Uuid7(length=16), nullable=False),
        sa.Column("registration_setting_id", app.db.types.Uuid7(length=16), nullable=False),
        sa.Column("branch_id", app.db.types.Uuid7(length=16), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column("id", app.db.types.Uuid7(length=16), nullable=False),
        sa.Column("created_at", app.db.types.UtcDateTime(), server_default=_NOW, nullable=False),
        sa.Column("created_by", app.db.types.Uuid7(length=16), nullable=True),
        sa.CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from",
            name="ck_statutory_reg_branches_interval",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_statutory_registration_branches_created_by"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_statutory_registration_branches_organization_id"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "registration_setting_id"],
            [
                "statutory_registration_settings.organization_id",
                "statutory_registration_settings.id",
            ],
            name="fk_statutory_reg_branches_setting",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "branch_id"],
            ["branches.organization_id", "branches.id"],
            name="fk_statutory_reg_branches_branch",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_statutory_registration_branches")),
        sa.UniqueConstraint("organization_id", "id", name="uq_statutory_reg_branches_org_id"),
        sa.UniqueConstraint(
            "registration_setting_id",
            "branch_id",
            "effective_from",
            name="uq_statutory_reg_branches_start",
        ),
    )
    op.create_index(
        "ix_statutory_reg_branches_branch",
        "statutory_registration_branches",
        ["organization_id", "branch_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_statutory_reg_branches_branch", table_name="statutory_registration_branches")
    op.drop_table("statutory_registration_branches")
    op.drop_table("statutory_registration_settings")
