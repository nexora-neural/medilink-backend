"""add lab upload request revocation reason

Revision ID: 74f97cc7d042
Revises: 7c28b98a7d35
Create Date: 2026-11-16 20:21:26.356992

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '74f97cc7d042'
down_revision: str | Sequence[str] | None = '7c28b98a7d35'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "lab_upload_requests",
        sa.Column("revocation_reason", sa.Text(), nullable=True),
    )
    pass


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("lab_upload_requests", "revocation_reason")
    pass
