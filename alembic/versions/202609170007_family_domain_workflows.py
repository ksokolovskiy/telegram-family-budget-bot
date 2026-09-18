"""family preferences, lifecycle invites, transaction audit and report indexes

Revision ID: 202609170007
Revises: 202609170006
Create Date: 2026-09-17
"""

from alembic import op
import sqlalchemy as sa


revision = "202609170007"
down_revision = "202609170006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "families",
        sa.Column("timezone", sa.String(length=64), nullable=False, server_default="Asia/Jerusalem"),
    )
    op.add_column(
        "families", sa.Column("currency", sa.String(length=3), nullable=False, server_default="RUB")
    )
    # Existing "member" accounts had read-only behaviour; preserve that behaviour
    # under the explicit viewer role.
    op.execute("UPDATE users SET role = 'viewer' WHERE role = 'member' OR role IS NULL")
    op.alter_column("users", "role", server_default="viewer")

    op.create_table(
        "family_invites",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("family_id", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(length=48), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False, server_default="viewer"),
        sa.Column("created_by_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by_id", sa.BigInteger(), nullable=True),
        sa.Column("uses_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_uses", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["family_id"], ["families.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["revoked_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code"),
    )
    op.create_index("ix_family_invites_code", "family_invites", ["code"], unique=True)
    op.create_index("ix_family_invites_expires_at", "family_invites", ["expires_at"])
    op.create_index("ix_family_invites_revoked_at", "family_invites", ["revoked_at"])
    op.create_index("ix_family_invites_family_active", "family_invites", ["family_id", "revoked_at", "expires_at"])

    op.add_column("transactions", sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("transactions", sa.Column("updated_by_id", sa.BigInteger(), nullable=True))
    op.add_column("transactions", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("transactions", sa.Column("deleted_by_id", sa.BigInteger(), nullable=True))
    op.add_column("transactions", sa.Column("deletion_reason", sa.Text(), nullable=True))
    op.create_foreign_key("fk_transactions_updated_by", "transactions", "users", ["updated_by_id"], ["id"], ondelete="SET NULL")
    op.create_foreign_key("fk_transactions_deleted_by", "transactions", "users", ["deleted_by_id"], ["id"], ondelete="SET NULL")
    op.create_index("ix_transactions_deleted_at", "transactions", ["deleted_at"])
    op.create_index("ix_transactions_family_date", "transactions", ["family_id", "date"])
    op.create_index("ix_transactions_family_category_date", "transactions", ["family_id", "category_id", "date"])
    op.create_index("ix_transactions_family_active_date", "transactions", ["family_id", "deleted_at", "date"])

    op.create_table(
        "transaction_audits",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("transaction_id", sa.Integer(), nullable=False),
        sa.Column("family_id", sa.Integer(), nullable=False),
        sa.Column("actor_id", sa.BigInteger(), nullable=True),
        sa.Column("action", sa.String(length=20), nullable=False),
        sa.Column("before", sa.JSON(), nullable=True),
        sa.Column("after", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["family_id"], ["families.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["transaction_id"], ["transactions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_transaction_audits_transaction_created", "transaction_audits", ["transaction_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_transaction_audits_transaction_created", table_name="transaction_audits")
    op.drop_table("transaction_audits")
    op.drop_index("ix_transactions_family_active_date", table_name="transactions")
    op.drop_index("ix_transactions_family_category_date", table_name="transactions")
    op.drop_index("ix_transactions_family_date", table_name="transactions")
    op.drop_index("ix_transactions_deleted_at", table_name="transactions")
    op.drop_constraint("fk_transactions_deleted_by", "transactions", type_="foreignkey")
    op.drop_constraint("fk_transactions_updated_by", "transactions", type_="foreignkey")
    op.drop_column("transactions", "deletion_reason")
    op.drop_column("transactions", "deleted_by_id")
    op.drop_column("transactions", "deleted_at")
    op.drop_column("transactions", "updated_by_id")
    op.drop_column("transactions", "updated_at")
    op.drop_index("ix_family_invites_family_active", table_name="family_invites")
    op.drop_index("ix_family_invites_revoked_at", table_name="family_invites")
    op.drop_index("ix_family_invites_expires_at", table_name="family_invites")
    op.drop_index("ix_family_invites_code", table_name="family_invites")
    op.drop_table("family_invites")
    op.alter_column("users", "role", server_default=None)
    op.drop_column("families", "currency")
    op.drop_column("families", "timezone")
