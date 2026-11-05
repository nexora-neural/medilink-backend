"""Add prescription review evidence.

Revision ID: rx_review_evidence
Revises: c2b1011a8d51
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "rx_review_evidence"
down_revision = "c2b1011a8d51"
branch_labels = None
depends_on = None

review_outcome = postgresql.ENUM(
    "reviewed",
    "approved",
    "needs_clarification",
    "not_accepted",
    name="review_outcome",
    create_type=False,
)


def upgrade() -> None:
    review_outcome.create(op.get_bind())
    op.create_table(
        "prescription_reviews",
        sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "prescription_id",
            sa.UUID(),
            sa.ForeignKey("prescriptions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "reviewer_user_id",
            sa.UUID(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("outcome", review_outcome, nullable=False),
        sa.Column("notes", sa.Text()),
        sa.Column(
            "reviewed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_prescription_reviews_history",
        "prescription_reviews",
        ["prescription_id", sa.literal_column("reviewed_at DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_prescription_reviews_history", table_name="prescription_reviews")
    op.drop_table("prescription_reviews")
    review_outcome.drop(op.get_bind())
