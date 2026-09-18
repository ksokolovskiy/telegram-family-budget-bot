"""set ILS as the currency for newly created families

Revision ID: 202609170011
Revises: 202609170010
Create Date: 2026-09-17
"""

from alembic import op


revision = "202609170011"
down_revision = "202609170010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE families ALTER COLUMN currency SET DEFAULT 'ILS'")


def downgrade() -> None:
    op.execute("ALTER TABLE families ALTER COLUMN currency SET DEFAULT 'RUB'")
