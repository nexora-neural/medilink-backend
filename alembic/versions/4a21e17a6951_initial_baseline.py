"""initial baseline

Revision ID: 4a21e17a6951
Revises:
Create Date: 2026-11-02 19:50:22.543604

"""
from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = '4a21e17a6951'
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
