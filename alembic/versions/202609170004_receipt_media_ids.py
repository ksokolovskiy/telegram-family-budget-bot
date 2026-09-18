"""retain Telegram receipt media identifiers

Revision ID: 202609170004
Revises: 202609170003
Create Date: 2026-09-17
"""

from alembic import op
import sqlalchemy as sa


revision = "202609170004"
down_revision = "202609170003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("transactions") as batch:
        batch.add_column(sa.Column("receipt_file_id", sa.String(255), nullable=True))
        batch.add_column(sa.Column("receipt_mime_type", sa.String(120), nullable=True))
    with op.batch_alter_table("transaction_drafts") as batch:
        batch.add_column(sa.Column("receipt_file_id", sa.String(255), nullable=True))
        batch.add_column(sa.Column("receipt_mime_type", sa.String(120), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("transaction_drafts") as batch:
        batch.drop_column("receipt_mime_type")
        batch.drop_column("receipt_file_id")
    with op.batch_alter_table("transactions") as batch:
        batch.drop_column("receipt_mime_type")
        batch.drop_column("receipt_file_id")
