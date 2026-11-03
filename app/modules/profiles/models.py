"""SQLAlchemy ORM models for user profiles."""

from sqlalchemy import Column, Date, DateTime, ForeignKey, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID

from app.database.base import Base


class PatientProfile(Base):
    __tablename__ = "patient_profiles"

    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        primary_key=True,
    )

    date_of_birth = Column(
        Date,
        nullable=False,
    )

    sex = Column(
        Text,
        nullable=False,
    )

    emergency_contact_name = Column(
        Text,
        nullable=True,
    )

    emergency_contact_phone = Column(
        Text,
        nullable=True,
    )

    notification_preferences = Column(
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    )

    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )