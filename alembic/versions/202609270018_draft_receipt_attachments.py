"""retain all media pages for a receipt draft retry

Revision ID: 202609270018
Revises: 202609180017
Create Date: 2026-09-27
"""

from alembic import op
import sqlalchemy as sa


revision = "202609270018"
down_revision = "202609180017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("transaction_drafts", sa.Column("receipt_attachments", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("transaction_drafts", "receipt_attachments")
