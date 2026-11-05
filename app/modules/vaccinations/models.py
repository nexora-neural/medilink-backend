"""Patient vaccination database model."""

from datetime import date, datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, Date, DateTime, ForeignKey, Index, Text, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.schema import conv

from app.database.base import Base
from app.modules.clinical_records.models import RecordSource, record_source_enum


class Vaccination(Base):
    __tablename__ = "vaccinations"
    __table_args__ = (
        CheckConstraint(
            "next_dose_on IS NULL OR next_dose_on >= administered_on",
            name=conv("ck_vaccine_next_dose"),
        ),
        CheckConstraint(
            "(supersedes_id IS NULL) = (correction_reason IS NULL)",
            name=conv("ck_vaccine_correction"),
        ),
        Index("ix_vaccinations_patient_date", "patient_user_id", text("administered_on DESC")),
        Index(
            "ix_vaccinations_next_dose",
            "next_dose_on",
            postgresql_where=text("next_dose_on IS NOT NULL"),
        ),
    )
    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    patient_user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("patient_profiles.user_id", ondelete="RESTRICT")
    )
    vaccine_name: Mapped[str] = mapped_column(Text)
    administered_on: Mapped[date] = mapped_column(Date)
    dose_details: Mapped[str | None] = mapped_column(Text)
    next_dose_on: Mapped[date | None] = mapped_column(Date)
    recorded_by: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT")
    )
    source: Mapped[RecordSource] = mapped_column(record_source_enum)
    organization_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT")
    )
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    supersedes_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("vaccinations.id", ondelete="RESTRICT")
    )
    correction_reason: Mapped[str | None] = mapped_column(Text)
