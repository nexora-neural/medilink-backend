"""add clinical record db change

Revision ID: 0e03abe27cd8
Revises: c1c2a172fbc4
Create Date: 2026-11-03 14:37:16.360841

"""
from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = '0e03abe27cd8'
down_revision: str | Sequence[str] | None = 'c1c2a172fbc4'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
