"""Merge profile and verification migration heads

Revision ID: 7525292d5d7a
Revises: d94be71f3a62, ml22_profiles
Create Date: 2026-11-04 19:39:12.633131

"""
from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = '7525292d5d7a'
down_revision: str | Sequence[str] | None = ('d94be71f3a62', 'ml22_profiles')
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
