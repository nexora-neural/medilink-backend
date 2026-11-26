"""Merge report withdrawal and professional signup migrations

Revision ID: 449942b93eaa
Revises: ml124_report_withdrawal, ml136_prof_signup
Create Date: 2026-11-26 09:18:02.763552

"""
from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = '449942b93eaa'
down_revision: str | Sequence[str] | None = ('ml124_report_withdrawal', 'ml136_prof_signup')
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
