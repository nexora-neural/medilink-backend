"""SQLAlchemy ORM models for medication schedules and adherence."""

import uuid
from datetime import datetime, time

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    Time,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ENUM, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base

from .enum import DoseResponse, DoseStatus, ScheduleStatus

# Reuse existing PostgreSQL enum types and store Python enum values.
schedule_status_enum = ENUM(
    ScheduleStatus,
    name="schedule_status",
    schema="public",
    create_type=False,
    values_callable=lambda cls: [member.value for member in cls],
    validate_strings=True,
)

dose_status_enum = ENUM(
    DoseStatus,
    name="dose_status",
    schema="public",
    create_type=False,
    values_callable=lambda cls: [member.value for member in cls],
    validate_strings=True,
)

dose_response_enum = ENUM(
    DoseResponse,
    name="dose_response",
    schema="public",
    create_type=False,
    values_callable=lambda cls: [member.value for member in cls],
    validate_strings=True,
)


class MedicationSchedule(Base):
    __tablename__ = "medication_schedules"

    __table_args__ = (
        CheckConstraint(
            "ends_at IS NULL OR ends_at >= starts_at",
            name="ck_schedule_window",
        ),
        Index(
            "ix_schedules_patient_status",
            "patient_user_id",
            "status",
            "starts_at",
        ),
        Index("ix_schedules_item", "prescription_item_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    prescription_item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("prescription_items.id", ondelete="RESTRICT"),
        nullable=False,
    )
    patient_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("patient_profiles.user_id", ondelete="RESTRICT"),
        nullable=False,
    )
    timezone: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )
    starts_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    ends_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    confirmed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    status: Mapped[ScheduleStatus] = mapped_column(
        schedule_status_enum,
        nullable=False,
        server_default=text("'active'::public.schedule_status"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )
    version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("1"),
    )

    schedule_times: Mapped[list["ScheduleTime"]] = relationship(
        back_populates="schedule",
    )
    dose_occurrences: Mapped[list["DoseOccurrence"]] = relationship(
        back_populates="schedule",
    )


class ScheduleTime(Base):
    __tablename__ = "schedule_times"

    __table_args__ = (
        UniqueConstraint(
            "schedule_id",
            "time_of_day",
            name="uq_schedule_time",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    schedule_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("medication_schedules.id", ondelete="RESTRICT"),
        nullable=False,
    )
    time_of_day: Mapped[time] = mapped_column(
        Time(timezone=False),
        nullable=False,
    )

    schedule: Mapped["MedicationSchedule"] = relationship(
        back_populates="schedule_times",
    )


class DoseOccurrence(Base):
    __tablename__ = "dose_occurrences"

    __table_args__ = (
        UniqueConstraint(
            "schedule_id",
            "scheduled_for",
            name="uq_dose_occurrence",
        ),
        Index(
            "ix_dose_occurrences_due",
            "status",
            "scheduled_for",
            postgresql_where=text("status IN ('upcoming', 'due')"),
        ),
        Index(
            "ix_dose_occurrences_schedule",
            "schedule_id",
            "scheduled_for",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    schedule_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("medication_schedules.id", ondelete="RESTRICT"),
        nullable=False,
    )
    scheduled_for: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    status: Mapped[DoseStatus] = mapped_column(
        dose_status_enum,
        nullable=False,
        server_default=text("'upcoming'::public.dose_status"),
    )
    snoozed_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )

    schedule: Mapped["MedicationSchedule"] = relationship(
        back_populates="dose_occurrences",
    )
    medication_logs: Mapped[list["MedicationLog"]] = relationship(
        back_populates="dose_occurrence",
    )


class MedicationLog(Base):
    __tablename__ = "medication_logs"

    __table_args__ = (
        CheckConstraint(
            "(correction_of_id IS NULL) = (correction_reason IS NULL)",
            name="ck_medication_log_correction",
        ),
        CheckConstraint(
            "response <> 'taken'::public.dose_response OR reported_taken_at IS NOT NULL",
            name="ck_medication_log_taken_time",
        ),
        Index(
            "ix_medication_logs_occurrence",
            "dose_occurrence_id",
            text("recorded_at DESC"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    dose_occurrence_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("dose_occurrences.id", ondelete="RESTRICT"),
        nullable=False,
    )
    response: Mapped[DoseResponse] = mapped_column(
        dose_response_enum,
        nullable=False,
    )
    reported_taken_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    recorded_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )
    correction_of_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("medication_logs.id", ondelete="RESTRICT"),
        nullable=True,
    )
    correction_reason: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    dose_occurrence: Mapped["DoseOccurrence"] = relationship(
        back_populates="medication_logs",
    )
    corrected_log: Mapped["MedicationLog | None"] = relationship(
        "MedicationLog",
        remote_side="MedicationLog.id",
        foreign_keys=[correction_of_id],
    )
