"""Add PostgreSQL-backed request rate limits."""

import sqlalchemy as sa

from alembic import op

revision = "0e11c6efc030"
down_revision = "a589b6ca63c7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "request_rate_limits",
        sa.Column("bucket_key", sa.String(64), primary_key=True),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("request_count", sa.Integer(), nullable=False),
        sa.CheckConstraint("request_count > 0", name=op.f("ck_request_rate_limits_positive_count")),
    )
    op.create_index("ix_request_rate_limits_window_end", "request_rate_limits", ["window_end"])


def downgrade() -> None:
    op.drop_table("request_rate_limits")
