"""owner-managed application settings

Revision ID: 202609170002
Revises: 202609170001
Create Date: 2026-09-17
"""
from alembic import op
import sqlalchemy as sa

revision = "202609170002"
down_revision = "202609170001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "app_settings",
        sa.Column("key", sa.String(length=64), primary_key=True),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("app_settings")
