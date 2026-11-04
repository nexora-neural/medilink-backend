"""SQLAlchemy ORM models for patient consent and access grants."""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    text,
)
from sqlalchemy.dialects.postgresql import ENUM, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


grant_status_enum = ENUM(
    "active",
    "expired",
    "revoked",
    name="grant_status",
    create_type=False,
)

grant_category_enum = ENUM(
    "history_allergies",
    "prescriptions",
    "reports",
    "measurements_vaccinations",
    "medication_logs",
    name="grant_category",
    create_type=False,
)


class AccessGrant(Base):
    __tablename__ = "access_grants"

    __table_args__ = (
        CheckConstraint(
            "expires_at > starts_at",
            name="ck_grant_window",
        ),
        CheckConstraint(
            "(status = 'revoked') = (revoked_at IS NOT NULL)",
            name="ck_grant_revoked",
        ),
        Index(
            "ix_grants_patient_status_expiry",
            "patient_user_id",
            "status",
            "expires_at",
        ),
        Index(
            "ix_grants_professional_status_expiry",
            "professional_user_id",
            "status",
            "expires_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )

    patient_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("patient_profiles.user_id", ondelete="RESTRICT"),
        nullable=False,
    )

    professional_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )

    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=True,
    )

    allow_consultation: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("false"),
    )

    status: Mapped[str] = mapped_column(
        grant_status_enum,
        nullable=False,
        server_default=text("'active'"),
    )

    starts_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )

    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )

    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )


class GrantCategory(Base):
    __tablename__ = "grant_categories"

    grant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("access_grants.id", ondelete="RESTRICT"),
        primary_key=True,
    )

    category: Mapped[str] = mapped_column(
        grant_category_enum,
        primary_key=True,
    )