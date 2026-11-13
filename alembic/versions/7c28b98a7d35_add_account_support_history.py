"""Retain revoked role assignments and private decision reasons."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "7c28b98a7d35"
down_revision = "0e11c6efc030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("user_roles", sa.Column("revoked_at", sa.DateTime(timezone=True)))
    op.create_table(
        "account_support_decision_reasons",
        sa.Column("audit_event_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["audit_event_id"],
            ["audit_events.id"],
            name="account_support_decision_reasons_audit_event_id_fkey",
            ondelete="RESTRICT",
        ),
    )


def downgrade() -> None:
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM user_roles WHERE revoked_at IS NOT NULL)
                OR EXISTS (SELECT 1 FROM account_support_decision_reasons)
            THEN
                RAISE EXCEPTION 'Account support history must be preserved before downgrade';
            END IF;
        END $$;
    """)
    op.drop_table("account_support_decision_reasons")
    op.drop_column("user_roles", "revoked_at")
