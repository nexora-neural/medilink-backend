"""Add measurement and correction API

Revision ID: 45d60b1d9188
Revises: 0e11c6efc030
Create Date: 2026-11-13 12:09:58.460505

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '45d60b1d9188'
down_revision: Union[str, Sequence[str], None] = '0e11c6efc030'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
