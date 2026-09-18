"""repair exchange-rate columns for databases previously stamped at revision 012

Revision ID: 202609170013
Revises: 202609170012
Create Date: 2026-09-17
"""

from alembic import op


revision = "202609170013"
down_revision = "202609170012"
branch_labels = None
depends_on = None


_COLUMNS = (
    ("source_amount", "NUMERIC(12, 2)"),
    ("source_currency", "VARCHAR(3)"),
    ("exchange_rate", "NUMERIC(16, 8)"),
    ("exchange_rate_date", "DATE"),
    ("exchange_rate_source", "VARCHAR(64)"),
)


def upgrade() -> None:
    # Some early local installations were marked as revision 012 before its
    # DDL reached PostgreSQL. IF NOT EXISTS makes this safe for both states.
    for table in ("transactions", "transaction_drafts"):
        for name, sql_type in _COLUMNS:
            op.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {name} {sql_type}")


def downgrade() -> None:
    for table in ("transaction_drafts", "transactions"):
        for name, _ in reversed(_COLUMNS):
            op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS {name}")
