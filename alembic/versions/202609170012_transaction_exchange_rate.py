"""store original foreign-currency transaction amounts and fixed BOI rates

Revision ID: 202609170012
Revises: 202609170011
Create Date: 2026-09-17
"""

from alembic import op
import sqlalchemy as sa


revision = "202609170012"
down_revision = "202609170011"
branch_labels = None
depends_on = None


def _columns() -> list[sa.Column]:
    return [
        sa.Column("source_amount", sa.Numeric(12, 2), nullable=True),
        sa.Column("source_currency", sa.String(3), nullable=True),
        sa.Column("exchange_rate", sa.Numeric(16, 8), nullable=True),
        sa.Column("exchange_rate_date", sa.Date(), nullable=True),
        sa.Column("exchange_rate_source", sa.String(64), nullable=True),
    ]


def upgrade() -> None:
    for table in ("transactions", "transaction_drafts"):
        for column in _columns():
            op.add_column(table, column)


def downgrade() -> None:
    for table in ("transaction_drafts", "transactions"):
        for name in ("exchange_rate_source", "exchange_rate_date", "exchange_rate", "source_currency", "source_amount"):
            op.drop_column(table, name)
