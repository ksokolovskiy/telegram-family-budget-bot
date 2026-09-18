"""receipt retention, recurring transactions, accounts, transfers and split groups

Revision ID: 202609170008
Revises: 202609170007
Create Date: 2026-09-17
"""

from alembic import op
import sqlalchemy as sa


revision = "202609170008"
down_revision = "202609170007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "families",
        sa.Column("receipt_retention_days", sa.Integer(), nullable=False, server_default="365"),
    )

    op.create_table(
        "transaction_groups",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("family_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("created_by_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["family_id"], ["families.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_transaction_groups_family_id", "transaction_groups", ["family_id"])

    op.add_column("transactions", sa.Column("group_id", sa.Integer(), nullable=True))
    op.add_column("transactions", sa.Column("receipt_retention_until", sa.DateTime(timezone=True), nullable=True))
    op.add_column("transactions", sa.Column("receipt_purged_at", sa.DateTime(timezone=True), nullable=True))
    op.create_foreign_key("fk_transactions_group", "transactions", "transaction_groups", ["group_id"], ["id"], ondelete="SET NULL")
    op.create_index("ix_transactions_receipt_retention_until", "transactions", ["receipt_retention_until"])

    op.create_table(
        "accounts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("family_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["family_id"], ["families.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("family_id", "name", name="uq_account_family_name"),
    )
    op.create_index("ix_accounts_family_id", "accounts", ["family_id"])
    op.create_table(
        "transfers",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("family_id", sa.Integer(), nullable=False),
        sa.Column("group_id", sa.Integer(), nullable=False),
        sa.Column("from_account_id", sa.Integer(), nullable=False),
        sa.Column("to_account_id", sa.Integer(), nullable=False),
        sa.Column("amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("date", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["family_id"], ["families.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["from_account_id"], ["accounts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["group_id"], ["transaction_groups.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["to_account_id"], ["accounts.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("group_id"),
    )
    op.create_index("ix_transfers_family_id", "transfers", ["family_id"])
    op.create_index("ix_transfers_date", "transfers", ["date"])

    op.create_table(
        "recurring_transactions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("family_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=True),
        sa.Column("category_id", sa.Integer(), nullable=False),
        sa.Column("amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("type", sa.String(length=20), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("cadence", sa.String(length=20), nullable=False),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["category_id"], ["categories.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["family_id"], ["families.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_recurring_transactions_due", "recurring_transactions", ["family_id", "is_active", "next_run_at"])


def downgrade() -> None:
    op.drop_index("ix_recurring_transactions_due", table_name="recurring_transactions")
    op.drop_table("recurring_transactions")
    op.drop_index("ix_transfers_date", table_name="transfers")
    op.drop_index("ix_transfers_family_id", table_name="transfers")
    op.drop_table("transfers")
    op.drop_index("ix_accounts_family_id", table_name="accounts")
    op.drop_table("accounts")
    op.drop_index("ix_transactions_receipt_retention_until", table_name="transactions")
    op.drop_constraint("fk_transactions_group", "transactions", type_="foreignkey")
    op.drop_column("transactions", "receipt_purged_at")
    op.drop_column("transactions", "receipt_retention_until")
    op.drop_column("transactions", "group_id")
    op.drop_index("ix_transaction_groups_family_id", table_name="transaction_groups")
    op.drop_table("transaction_groups")
    op.drop_column("families", "receipt_retention_days")
