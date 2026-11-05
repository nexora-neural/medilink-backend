"""SQLAlchemy ORM models for user profiles."""

from sqlalchemy import CheckConstraint, Column, Date, DateTime, ForeignKey, Index, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.schema import conv

from app.database.base import Base
from app.modules.identity.enums import professional_role_type, verification_status_type


class PatientProfile(Base):
    __tablename__ = "patient_profiles"
    __table_args__ = (CheckConstraint("sex IN ('female','male')", name=conv("ck_patient_sex")),)

    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    date_of_birth = Column(Date, nullable=False)
    sex = Column(Text, nullable=False)
    emergency_contact_name = Column(Text, nullable=True)
    emergency_contact_phone = Column(Text, nullable=True)
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

class ProfessionalProfile(Base):
    """Store the professional profile linked to a local user."""

    __tablename__ = "professional_profiles"
    __table_args__ = (
        CheckConstraint(
            "(role = 'doctor'::professional_role) = (doctor_reference IS NOT NULL)",
            name="ck_doctor_reference_role",
        ),
        Index("ix_professional_registration", "role", "registration_number"),
    )

    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey(
            "users.id",
            name="professional_profiles_user_id_fkey",
            ondelete="RESTRICT",
        ),
        primary_key=True,
    )
    role = Column(professional_role_type, nullable=False)
    doctor_reference = Column(Text, unique=True)
    registration_number = Column(Text)
    qualifications = Column(Text)
    specialty = Column(Text)
    verification_status = Column(
        verification_status_type,
        nullable=False,
        server_default=text("'not_submitted'::verification_status"),
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
