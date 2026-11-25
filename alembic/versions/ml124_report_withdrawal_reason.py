"""Retain report withdrawal reasons.

Revision ID: ml124_report_withdrawal
Revises: a104c116f001
"""
import sqlalchemy as sa

from alembic import op

revision = "ml124_report_withdrawal"
down_revision = "a104c116f001"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("reports", sa.Column("withdrawal_reason", sa.Text(), nullable=True))


def downgrade():
    op.drop_column("reports", "withdrawal_reason")
