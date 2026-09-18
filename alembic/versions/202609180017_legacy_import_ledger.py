"""record imported and intentionally skipped legacy rows

Revision ID: 202609180017
Revises: 202609180016
Create Date: 2026-09-18
"""

import sqlalchemy as sa
from alembic import op


revision = "202609180017"
down_revision = "202609180016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "legacy_import_rows",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("family_id", sa.Integer(), sa.ForeignKey("families.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_key", sa.String(length=64), nullable=False),
        sa.Column("source_file", sa.String(length=255), nullable=False),
        sa.Column("source_sheet", sa.String(length=120), nullable=False),
        sa.Column("source_row", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("transaction_id", sa.Integer(), sa.ForeignKey("transactions.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("family_id", "source_key", name="uq_legacy_import_rows_family_source"),
    )
    op.create_index("ix_legacy_import_rows_family_id", "legacy_import_rows", ["family_id"])


def downgrade() -> None:
    op.drop_index("ix_legacy_import_rows_family_id", table_name="legacy_import_rows")
    op.drop_table("legacy_import_rows")
