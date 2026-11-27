"""Persist worker scheduling order for fair batch rotation."""

import sqlalchemy as sa

from alembic import op

revision = "ml170_worker_fairness"
down_revision = "ml170_occurrences"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "medication_schedules",
        sa.Column("occurrences_worker_processed_at", sa.DateTime(timezone=True)),
    )


def downgrade():
    op.drop_column("medication_schedules", "occurrences_worker_processed_at")
