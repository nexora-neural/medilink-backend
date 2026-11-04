"""add orm_document_models

Revision ID: 400eb0c99bb0
Revises: c1c2a172fbc4
Create Date: 2026-11-04 19:18:23.601724

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '400eb0c99bb0'
down_revision: Union[str, Sequence[str], None] = 'c1c2a172fbc4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
