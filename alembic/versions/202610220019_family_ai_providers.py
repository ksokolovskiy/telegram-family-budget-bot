"""Independent family provider credentials; preserve existing OpenAI settings."""
from alembic import op
import sqlalchemy as sa

revision = "202610220019"
down_revision = "202609270018"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("families", sa.Column("ai_provider", sa.String(16), nullable=False, server_default="openai"))
    op.add_column("families", sa.Column("google_api_key", sa.Text(), nullable=True))
    op.add_column("families", sa.Column("google_api_key_encryption_version", sa.String(32), nullable=True))
    op.add_column("families", sa.Column("google_model", sa.String(120), nullable=False, server_default="gemini-4-argon"))


def downgrade():
    for name in ("google_model", "google_api_key_encryption_version", "google_api_key", "ai_provider"):
        op.drop_column("families", name)
