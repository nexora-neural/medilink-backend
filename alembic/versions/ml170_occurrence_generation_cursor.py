"""Track the generated-through instant for medication occurrences.

Revision ID: ml170_occurrences
Revises: 449942b93eaa, 9b91626ba3f0
"""

import sqlalchemy as sa

from alembic import op

revision = "ml170_occurrences"
down_revision = ("449942b93eaa", "9b91626ba3f0")
branch_labels = None
depends_on = None


def upgrade():
    # Existing schedules begin generation at deployment time; this avoids
    # inventing adherence history for periods before the occurrence worker.
    op.add_column(
        "medication_schedules",
        sa.Column(
            "occurrences_generated_through",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )


def downgrade():
    op.drop_column("medication_schedules", "occurrences_generated_through")
