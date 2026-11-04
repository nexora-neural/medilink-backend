"""Add consent grants and consultations."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "e46826be0a38"
down_revision = "7525292d5d7a"
branch_labels = None
depends_on = None


grant_status_enum = postgresql.ENUM(
    "active",
    "expired",
    "revoked",
    name="grant_status",
    create_type=False,
)

grant_category_enum = postgresql.ENUM(
    "history_allergies",
    "prescriptions",
    "reports",
    "measurements_vaccinations",
    "medication_logs",
    name="grant_category",
    create_type=False,
)


def upgrade() -> None:
    bind = op.get_bind()

    grant_status_enum.create(bind, checkfirst=False)
    grant_category_enum.create(bind, checkfirst=False)

    op.create_table(
        "access_grants",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column(
            "patient_user_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "professional_user_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column(
            "allow_consultation",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "status",
            grant_status_enum,
            server_default=sa.text("'active'"),
            nullable=False,
        ),
        sa.Column(
            "starts_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "revoked_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["patient_user_id"],
            ["patient_profiles.user_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["professional_user_id"],
            ["users.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "expires_at > starts_at",
            name="ck_grant_window",
        ),
        sa.CheckConstraint(
            "(status = 'revoked') = (revoked_at IS NOT NULL)",
            name="ck_grant_revoked",
        ),
    )

    op.create_index(
        "ix_grants_patient_status_expiry",
        "access_grants",
        ["patient_user_id", "status", "expires_at"],
    )

    op.create_index(
        "ix_grants_professional_status_expiry",
        "access_grants",
        ["professional_user_id", "status", "expires_at"],
    )

    op.create_table(
        "grant_categories",
        sa.Column(
            "grant_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "category",
            grant_category_enum,
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("grant_id", "category"),
        sa.ForeignKeyConstraint(
            ["grant_id"],
            ["access_grants.id"],
            ondelete="RESTRICT",
        ),
    )

    op.create_table(
        "consultations",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column(
            "patient_user_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "doctor_user_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column(
            "grant_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "ended_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "summary",
            sa.Text(),
            nullable=True,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["patient_user_id"],
            ["patient_profiles.user_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["doctor_user_id"],
            ["users.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["grant_id"],
            ["access_grants.id"],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "ended_at IS NULL OR ended_at >= started_at",
            name="ck_consultation_window",
        ),
    )

    op.create_index(
        "ix_consultations_patient_date",
        "consultations",
        ["patient_user_id", sa.text("started_at DESC")],
    )

    op.create_index(
        "ix_consultations_doctor_date",
        "consultations",
        ["doctor_user_id", sa.text("started_at DESC")],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_consultations_doctor_date",
        table_name="consultations",
    )
    op.drop_index(
        "ix_consultations_patient_date",
        table_name="consultations",
    )
    op.drop_table("consultations")

    op.drop_table("grant_categories")

    op.drop_index(
        "ix_grants_professional_status_expiry",
        table_name="access_grants",
    )
    op.drop_index(
        "ix_grants_patient_status_expiry",
        table_name="access_grants",
    )
    op.drop_table("access_grants")

    bind = op.get_bind()
    grant_category_enum.drop(bind, checkfirst=False)
    grant_status_enum.drop(bind, checkfirst=False)
