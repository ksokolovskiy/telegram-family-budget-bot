"""scope OpenAI credentials and model selection to each family

Revision ID: 202609170014
Revises: 202609170013
Create Date: 2026-09-17
"""

from alembic import op
import sqlalchemy as sa


revision = "202609170014"
down_revision = "202609170013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("families", sa.Column("openai_api_key", sa.Text(), nullable=True))
    op.add_column("families", sa.Column("openai_api_key_encryption_version", sa.String(32), nullable=True))
    op.add_column("families", sa.Column("openai_api_key_encrypted_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "families",
        sa.Column("openai_model", sa.String(120), nullable=False, server_default="gpt-5.4"),
    )


def downgrade() -> None:
    op.drop_column("families", "openai_model")
    op.drop_column("families", "openai_api_key_encrypted_at")
    op.drop_column("families", "openai_api_key_encryption_version")
    op.drop_column("families", "openai_api_key")
