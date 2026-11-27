"""Retain medication schedule replacement lineage and status reasons.

Revision ID: ml155_schedule_history
Revises: ml135_merge_heads
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "ml155_schedule_history"
down_revision = "ml135_merge_heads"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "medication_schedules", sa.Column("previous_schedule_id", sa.UUID(), nullable=True)
    )
    op.create_foreign_key(
        "fk_schedule_previous",
        "medication_schedules",
        "medication_schedules",
        ["previous_schedule_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_unique_constraint(
        "uq_medication_schedules_previous_schedule_id",
        "medication_schedules",
        ["previous_schedule_id"],
    )
    status = postgresql.ENUM(
        "active", "paused", "completed", "cancelled", name="schedule_status", create_type=False
    )
    op.create_table(
        "medication_schedule_status_changes",
        sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "schedule_id",
            sa.UUID(),
            sa.ForeignKey("medication_schedules.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "recorded_by", sa.UUID(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column("previous_status", status, nullable=False),
        sa.Column("status", status, nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_medication_schedule_status_changes_schedule_id",
        "medication_schedule_status_changes",
        ["schedule_id"],
    )


def downgrade():
    op.drop_table("medication_schedule_status_changes")
    op.drop_constraint(
        "uq_medication_schedules_previous_schedule_id", "medication_schedules", type_="unique"
    )
    op.drop_constraint(
        "fk_schedule_previous",
        "medication_schedules",
        type_="foreignkey",
    )
    op.drop_column("medication_schedules", "previous_schedule_id")
