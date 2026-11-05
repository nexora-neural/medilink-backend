"""Add patient and professional profile tables."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "ml22_profiles"
down_revision = "2e29f5568481"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the profile tables using the existing identity enums."""
    op.create_table(
        "patient_profiles",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("date_of_birth", sa.Date(), nullable=False),
        sa.Column("sex", sa.Text(), nullable=False),
        sa.Column("emergency_contact_name", sa.Text(), nullable=True),
        sa.Column("emergency_contact_phone", sa.Text(), nullable=True),
        sa.Column(
            "notification_preferences",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("user_id"),
        sa.CheckConstraint("sex IN ('female','male')", name=op.f("ck_patient_sex")),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
    )

    op.create_table(
        "professional_profiles",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "role",
            postgresql.ENUM(name="professional_role", create_type=False),
            nullable=False,
        ),
        sa.Column("doctor_reference", sa.Text(), nullable=True),
        sa.Column("registration_number", sa.Text(), nullable=True),
        sa.Column("qualifications", sa.Text(), nullable=True),
        sa.Column("specialty", sa.Text(), nullable=True),
        sa.Column(
            "verification_status",
            postgresql.ENUM(name="verification_status", create_type=False),
            server_default=sa.text("'not_submitted'::verification_status"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("user_id"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="professional_profiles_user_id_fkey",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("doctor_reference"),
        sa.CheckConstraint(
            "(role = 'doctor'::professional_role) = (doctor_reference IS NOT NULL)",
            name="ck_doctor_reference_role",
        ),
    )

    op.create_index(
        "ix_professional_registration",
        "professional_profiles",
        ["role", "registration_number"],
    )


def downgrade() -> None:
    """Remove the profile tables and their indexes."""
    op.drop_table("professional_profiles")
    op.drop_table("patient_profiles")
