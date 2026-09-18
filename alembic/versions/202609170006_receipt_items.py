"""retain classified receipt lines in the confirmation draft

Revision ID: 202609170006
Revises: 202609170005
Create Date: 2026-09-17
"""

from alembic import op
import sqlalchemy as sa


revision = "202609170006"
down_revision = "202609170005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("transaction_drafts", sa.Column("items", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("transaction_drafts", "items")
