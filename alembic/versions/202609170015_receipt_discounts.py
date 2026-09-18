"""persist whole-receipt discount on a confirmation draft

Revision ID: 202609170015
Revises: 202609170014
Create Date: 2026-09-17
"""

from alembic import op
import sqlalchemy as sa


revision = "202609170015"
down_revision = "202609170014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "transaction_drafts",
        sa.Column("receipt_discount", sa.Numeric(12, 2), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("transaction_drafts", "receipt_discount")
