"""make transaction confirmation drafts versioned and idempotent

Revision ID: 202609170003
Revises: 202609170002
Create Date: 2026-09-17
"""

from alembic import op
import sqlalchemy as sa


revision = "202609170003"
down_revision = "202609170002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Fresh installations receive these columns from 202609170001. This
    # migration exists for databases created by an earlier candidate build.
    with op.batch_alter_table("transaction_drafts") as batch:
        batch.add_column(sa.Column("revision", sa.Integer(), nullable=False, server_default="1"))
        batch.add_column(sa.Column("status", sa.String(20), nullable=False, server_default="active"))


def downgrade() -> None:
    with op.batch_alter_table("transaction_drafts") as batch:
        batch.drop_column("status")
        batch.drop_column("revision")
