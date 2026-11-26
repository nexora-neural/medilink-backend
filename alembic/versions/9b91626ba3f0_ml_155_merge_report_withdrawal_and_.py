"""ML-155: Merge report withdrawal and schedule history heads

Revision ID: 9b91626ba3f0
Revises: ml124_report_withdrawal, ml155_schedule_history
Create Date: 2026-11-25 22:49:05.346985

"""
from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = '9b91626ba3f0'
down_revision: str | Sequence[str] | None = ('ml124_report_withdrawal', 'ml155_schedule_history')
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
