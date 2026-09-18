"""use gpt-5.4 as the default family receipt model

Revision ID: 202609180016
Revises: 202609170015
Create Date: 2026-09-18
"""

from alembic import op


revision = "202609180016"
down_revision = "202609170015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("families", "openai_model", server_default="gpt-5.4")


def downgrade() -> None:
    op.alter_column("families", "openai_model", server_default="gpt-5-mini")
