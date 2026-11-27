"""Persist professional signup details through verification review."""

import sqlalchemy as sa

from alembic import op

revision = "ml136_prof_signup"
down_revision = "ml135_merge_heads"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("professional_profiles", sa.Column("workplace", sa.Text(), nullable=True))
    op.add_column("verification_applications", sa.Column("workplace", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("verification_applications", "workplace")
    op.drop_column("professional_profiles", "workplace")
