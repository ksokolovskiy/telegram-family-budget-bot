"""allow categories to be archived without losing history

Revision ID: 202609170005
Revises: 202609170004
Create Date: 2026-09-17
"""

from alembic import op
import sqlalchemy as sa


revision = "202609170005"
down_revision = "202609170004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("categories", sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()))


def downgrade() -> None:
    op.drop_column("categories", "is_active")
