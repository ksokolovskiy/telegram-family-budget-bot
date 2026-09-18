"""persistent rich message screens and transaction drafts

Revision ID: 202609170001
Revises: 202607120001
Create Date: 2026-09-17
"""
from alembic import op
import sqlalchemy as sa

revision = "202609170001"
down_revision = "202607120001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "interactive_screens",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("token", sa.String(32), nullable=False, unique=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("family_id", sa.Integer(), sa.ForeignKey("families.id", ondelete="CASCADE"), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("message_id", sa.Integer(), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("state", sa.JSON(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_interactive_screens_token", "interactive_screens", ["token"], unique=True)
    op.create_index("ix_interactive_screens_expires_at", "interactive_screens", ["expires_at"])
    op.create_table(
        "transaction_drafts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("token", sa.String(32), nullable=False, unique=True),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("family_id", sa.Integer(), sa.ForeignKey("families.id", ondelete="CASCADE"), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("message_id", sa.Integer(), nullable=True),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("type", sa.String(20), nullable=False),
        sa.Column("category", sa.String(120), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_transaction_drafts_token", "transaction_drafts", ["token"], unique=True)


def downgrade() -> None:
    op.drop_table("transaction_drafts")
    op.drop_table("interactive_screens")
