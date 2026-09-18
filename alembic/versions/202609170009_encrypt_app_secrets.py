"""record encryption metadata for persisted application secrets

Existing OpenAI keys intentionally remain readable plaintext during this
migration. Alembic runs before the bot's runtime settings are available, so it
must not depend on BOT_TOKEN. The next owner save re-encrypts the key with the
BOT_TOKEN-derived key and records its version.

Revision ID: 202609170009
Revises: 202609170008
Create Date: 2026-09-17
"""

from alembic import op
import sqlalchemy as sa


revision = "202609170009"
down_revision = "202609170008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("app_settings", sa.Column("encryption_version", sa.String(length=32), nullable=True))
    op.add_column("app_settings", sa.Column("encrypted_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("app_settings", "encrypted_at")
    op.drop_column("app_settings", "encryption_version")
