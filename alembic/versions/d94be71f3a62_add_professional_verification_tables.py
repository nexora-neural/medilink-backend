"""add professional verification tables

Revision ID: d94be71f3a62
Revises: 2e29f5568481
Create Date: 2026-11-04

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d94be71f3a62"
down_revision: str | Sequence[str] | None = "2e29f5568481"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create professional verification tables."""
    op.create_table(
        "verification_applications",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=True),
        sa.Column(
            "role",
            postgresql.ENUM(
                "doctor",
                "pharmacist",
                "lab_staff",
                name="professional_role",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("registration_number", sa.Text(), nullable=True),
        sa.Column(
            "status",
            postgresql.ENUM(
                "not_submitted",
                "pending",
                "approved",
                "rejected",
                "more_info_requested",
                "suspended",
                name="verification_status",
                create_type=False,
            ),
            server_default=sa.text("'pending'::verification_status"),
            nullable=False,
        ),
        sa.Column(
            "submitted_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("reviewed_by", sa.UUID(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_reason", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "(reviewed_by IS NULL) = (reviewed_at IS NULL)",
            name="ck_verification_review_pair",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="verification_applications_organization_id_fkey",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["reviewed_by"],
            ["users.id"],
            name="verification_applications_reviewed_by_fkey",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="verification_applications_user_id_fkey",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="verification_applications_pkey"),
    )
    op.create_index(
        "ix_verification_queue",
        "verification_applications",
        ["status", "submitted_at"],
    )
    op.create_index(
        "ix_verification_user",
        "verification_applications",
        ["user_id", sa.text("submitted_at DESC")],
    )
    op.create_table(
        "verification_decisions",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("application_id", sa.UUID(), nullable=False),
        sa.Column("reviewer_user_id", sa.UUID(), nullable=False),
        sa.Column("decision", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "decided_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "decision IN ('approved','rejected','more_info_requested','suspended')",
            name="ck_verification_decision",
        ),
        sa.ForeignKeyConstraint(
            ["application_id"],
            ["verification_applications.id"],
            name="verification_decisions_application_id_fkey",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["reviewer_user_id"],
            ["users.id"],
            name="verification_decisions_reviewer_user_id_fkey",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="verification_decisions_pkey"),
    )
    op.create_index(
        "ix_verification_decisions_history",
        "verification_decisions",
        ["application_id", sa.text("decided_at DESC")],
    )
    # Decision history is append-only, including for direct SQL writes.
    op.execute("""
        CREATE FUNCTION prevent_verification_decision_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'verification decision history is append-only'
                USING ERRCODE = '55000';
        END;
        $$
    """)
    op.execute("""
        CREATE TRIGGER verification_decisions_no_update_delete
        BEFORE UPDATE OR DELETE ON verification_decisions
        FOR EACH ROW EXECUTE FUNCTION prevent_verification_decision_mutation()
    """)
    op.execute("""
        CREATE TRIGGER verification_decisions_no_truncate
        BEFORE TRUNCATE ON verification_decisions
        FOR EACH STATEMENT EXECUTE FUNCTION prevent_verification_decision_mutation()
    """)


def downgrade() -> None:
    """Drop professional verification tables."""
    op.drop_index(
        "ix_verification_decisions_history", table_name="verification_decisions"
    )
    op.drop_table("verification_decisions")
    op.execute("DROP FUNCTION prevent_verification_decision_mutation()")
    op.drop_index("ix_verification_user", table_name="verification_applications")
    op.drop_index("ix_verification_queue", table_name="verification_applications")
    op.drop_table("verification_applications")
