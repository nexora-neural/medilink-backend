
"""SQLAlchemy ORM models for clinical records."""

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.schema import conv

from app.database.base import Base


class RecordSource(StrEnum):
    PATIENT = "patient"
    DOCTOR = "doctor"
    PHARMACIST = "pharmacist"
    LAB_STAFF = "lab_staff"
    APPROVED_EXTERNAL_SYSTEM = "approved_external_system"
    ADMIN = "admin"


class ClinicalRecordStatus(StrEnum):
    ACTIVE = "active"
    ENTERED_IN_ERROR = "entered_in_error"
    SUPERSEDED = "superseded"
    RESOLVED = "resolved"


record_source_enum = Enum(
    RecordSource,
    name="record_source",
    values_callable=lambda enum_cls: [item.value for item in enum_cls],
)

clinical_record_status_enum = Enum(
    ClinicalRecordStatus,
    name="clinical_record_status",
    values_callable=lambda enum_cls: [item.value for item in enum_cls],
)


class ClinicalRecordMixin:
    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )

    patient_user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("patient_profiles.user_id", ondelete="RESTRICT"),
        nullable=False,
    )

    consultation_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("consultations.id", ondelete="RESTRICT"),
    )

    source: Mapped[RecordSource] = mapped_column(
        record_source_enum,
        nullable=False,
    )

    recorded_by: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )

    organization_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="RESTRICT"),
    )

    status: Mapped[ClinicalRecordStatus] = mapped_column(
        clinical_record_status_enum,
        nullable=False,
        server_default=text("'active'"),
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )

    correction_reason: Mapped[str | None] = mapped_column(Text)


class ConditionsModel(ClinicalRecordMixin, Base):
    __tablename__ = "conditions"

    __table_args__ = (
        CheckConstraint(
            "(supersedes_id IS NULL) = (correction_reason IS NULL)",
            name=conv("ck_condition_correction"),
        ),
        Index(
            "ix_conditions_patient_date",
            "patient_user_id",
            text("recorded_at DESC"),
        ),
        Index(
            "ix_conditions_consultation",
            "consultation_id",
            postgresql_where=text("consultation_id IS NOT NULL"),
        ),
    )

    condition_name: Mapped[str] = mapped_column(Text, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    clinical_code: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)

    supersedes_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("conditions.id", ondelete="RESTRICT"),
    )


class DiagnosesModel(ClinicalRecordMixin, Base):
    __tablename__ = "diagnoses"

    __table_args__ = (
        CheckConstraint(
            "(supersedes_id IS NULL) = (correction_reason IS NULL)",
            name=conv("ck_diagnosis_correction"),
        ),
        Index(
            "ix_diagnoses_patient_date",
            "patient_user_id",
            text("diagnosed_at DESC"),
        ),
        Index(
            "ix_diagnoses_consultation",
            "consultation_id",
            postgresql_where=text("consultation_id IS NOT NULL"),
        ),
    )

    diagnosis_name: Mapped[str] = mapped_column(Text, nullable=False)
    clinical_code: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)

    diagnosed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )

    supersedes_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("diagnoses.id", ondelete="RESTRICT"),
    )


class AllergiesModel(ClinicalRecordMixin, Base):
    __tablename__ = "allergies"

    __table_args__ = (
        CheckConstraint(
            "(supersedes_id IS NULL) = (correction_reason IS NULL)",
            name=conv("ck_allergy_correction"),
        ),
        Index(
            "ix_allergies_patient_date",
            "patient_user_id",
            text("recorded_at DESC"),
        ),
        Index(
            "ix_allergies_consultation",
            "consultation_id",
            postgresql_where=text("consultation_id IS NOT NULL"),
        ),
    )

    allergen: Mapped[str] = mapped_column(Text, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reaction: Mapped[str | None] = mapped_column(Text)
    severity: Mapped[str | None] = mapped_column(Text)

    supersedes_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("allergies.id", ondelete="RESTRICT"),
    )


class ClinicalNotesModel(ClinicalRecordMixin, Base):
    __tablename__ = "clinical_notes"

    __table_args__ = (
        CheckConstraint(
            "(supersedes_id IS NULL) = (correction_reason IS NULL)",
            name=conv("ck_clinical_note_correction"),
        ),
        Index(
            "ix_clinical_notes_patient_date",
            "patient_user_id",
            text("recorded_at DESC"),
        ),
        Index(
            "ix_clinical_notes_consultation",
            "consultation_id",
            postgresql_where=text("consultation_id IS NOT NULL"),
        ),
    )

    note_type: Mapped[str] = mapped_column(Text, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    note_text: Mapped[str] = mapped_column(Text, nullable=False)

    supersedes_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("clinical_notes.id", ondelete="RESTRICT"),
    )


class RecordCorrectionsModel(Base):
    __tablename__ = "record_corrections"

    __table_args__ = (
        CheckConstraint(
            "num_nonnulls(condition_id, diagnosis_id, allergy_id, clinical_note_id) = 1",
            name=conv("ck_correction_one_target"),
        ),
        CheckConstraint(
            "(replacement_record_type IS NULL) = (replacement_record_id IS NULL)",
            name=conv("ck_correction_replacement_pair"),
        ),
        CheckConstraint(
            "request_status IN ('open','accepted','rejected','resolved')",
            name=conv("ck_correction_status"),
        ),
        Index(
            "ix_corrections_condition",
            "condition_id",
            text("created_at DESC"),
            postgresql_where=text("condition_id IS NOT NULL"),
        ),
        Index(
            "ix_corrections_diagnosis",
            "diagnosis_id",
            text("created_at DESC"),
            postgresql_where=text("diagnosis_id IS NOT NULL"),
        ),
        Index(
            "ix_corrections_allergy",
            "allergy_id",
            text("created_at DESC"),
            postgresql_where=text("allergy_id IS NOT NULL"),
        ),
        Index(
            "ix_corrections_note",
            "clinical_note_id",
            text("created_at DESC"),
            postgresql_where=text("clinical_note_id IS NOT NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )

    condition_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("conditions.id", ondelete="RESTRICT"),
    )

    diagnosis_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("diagnoses.id", ondelete="RESTRICT"),
    )

    allergy_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("allergies.id", ondelete="RESTRICT"),
    )

    clinical_note_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("clinical_notes.id", ondelete="RESTRICT"),
    )

    requested_by: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )

    reason: Mapped[str] = mapped_column(Text, nullable=False)

    request_status: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default=text("'open'"),
    )

    reviewed_by: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
    )

    reviewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
    )

    resolution_reason: Mapped[str | None] = mapped_column(Text)

    replacement_record_type: Mapped[str | None] = mapped_column(Text)

    replacement_record_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )
