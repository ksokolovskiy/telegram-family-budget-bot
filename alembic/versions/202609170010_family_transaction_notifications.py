"""enable family transaction notifications by default

Revision ID: 202609170010
Revises: 202609170009
Create Date: 2026-09-17
"""

from alembic import op
import sqlalchemy as sa


revision = "202609170010"
down_revision = "202609170009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "families",
        sa.Column(
            "notify_members_on_transactions",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )


def downgrade() -> None:
    op.drop_column("families", "notify_members_on_transactions")
