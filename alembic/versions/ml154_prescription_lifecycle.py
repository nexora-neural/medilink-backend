"""Retain private prescription lifecycle reasons and version action history."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "ml154_lifecycle"
down_revision = "ml170_worker_fairness"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "prescription_lifecycle_events",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "prescription_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("prescriptions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "related_prescription_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("prescriptions.id", ondelete="RESTRICT"),
        ),
        sa.Column(
            "actor_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("previous_status", sa.Text()),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("previous_version", sa.Integer(), nullable=False),
        sa.Column("new_version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "action IN ('version','renewal','reissue','cancel','discontinue',"
            "'complete','superseded')",
            name="prescription_lifecycle_action",
        ),
        sa.CheckConstraint(
            "length(btrim(reason)) BETWEEN 1 AND 1000", name="prescription_lifecycle_reason"
        ),
        sa.CheckConstraint(
            "previous_version >= 0 AND new_version > previous_version",
            name="prescription_lifecycle_version",
        ),
    )
    op.create_index(
        "ix_prescription_lifecycle_history",
        "prescription_lifecycle_events",
        ["prescription_id", "created_at", "id"],
    )


def downgrade():
    op.drop_index("ix_prescription_lifecycle_history", table_name="prescription_lifecycle_events")
    op.drop_table("prescription_lifecycle_events")
