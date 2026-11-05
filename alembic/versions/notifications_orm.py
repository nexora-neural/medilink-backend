"""Add notification persistence.

Revision ID: notifications_orm
Revises: c2b1011a8d51
"""

import sqlalchemy as sa

from alembic import op

revision = "notifications_orm"
down_revision = "c2b1011a8d51"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "notifications",
        sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "user_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column("notification_type", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("related_type", sa.Text()),
        sa.Column("related_id", sa.UUID()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("read_at", sa.DateTime(timezone=True)),
        sa.Column("dedupe_key", sa.Text(), nullable=False),
        sa.UniqueConstraint("dedupe_key"),
    )
    op.create_index(
        "ix_notifications_user_unread",
        "notifications",
        ["user_id", sa.literal_column("created_at DESC")],
        postgresql_where=sa.text("read_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_notifications_user_unread", table_name="notifications")
    op.drop_table("notifications")
