"""Shared medicine catalogue and prescription database models."""

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import ENUM
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.schema import conv

from app.database.base import Base


class PrescriptionSource(StrEnum):
    PATIENT = "patient"
    DOCTOR = "doctor"
    APPROVED_EXTERNAL_SYSTEM = "approved_external_system"


class PrescriptionReviewStatus(StrEnum):
    NOT_APPLICABLE = "not_applicable"
    NOT_REVIEWED = "not_reviewed"
    REVIEWED = "reviewed"
    APPROVED = "approved"
    NEEDS_CLARIFICATION = "needs_clarification"
    NOT_ACCEPTED = "not_accepted"


class PrescriptionStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    COMPLETED = "completed"
    DISCONTINUED = "discontinued"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"


def enum_type(enum_class, name):
    return ENUM(enum_class, name=name, values_callable=lambda cls: [item.value for item in cls])


class Medicine(Base):
    __tablename__ = "medicines"
    __table_args__ = (
        Index("ix_medicines_generic_name", text("lower(generic_name)")),
        Index(
            "ix_medicines_brand_name",
            text("lower(brand_name)"),
            postgresql_where=text("brand_name IS NOT NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    generic_name: Mapped[str] = mapped_column(Text, nullable=False)
    brand_name: Mapped[str | None] = mapped_column(Text)
    strength: Mapped[str | None] = mapped_column(Text)
    form: Mapped[str | None] = mapped_column(Text)
    is_sample: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class Prescription(Base):
    __tablename__ = "prescriptions"
    __table_args__ = (
        CheckConstraint(
            "ends_on IS NULL OR starts_on IS NULL OR ends_on >= starts_on",
            name=conv("ck_prescription_dates"),
        ),
        CheckConstraint(
            "source <> 'doctor' OR prescriber_user_id IS NOT NULL",
            name=conv("ck_prescription_issuer"),
        ),
        CheckConstraint(
            "status NOT IN ('active','completed','discontinued','cancelled','superseded') "
            "OR issued_at IS NOT NULL",
            name=conv("ck_prescription_issue_time"),
        ),
        CheckConstraint(
            "(reviewed_by IS NULL) = (reviewed_at IS NULL)",
            name=conv("ck_prescription_review_pair"),
        ),
        Index(
            "ix_prescriptions_patient_status", "patient_user_id", "status", text("created_at DESC")
        ),
        Index(
            "ix_prescriptions_prescriber",
            "prescriber_user_id",
            text("created_at DESC"),
            postgresql_where=text("prescriber_user_id IS NOT NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    patient_user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("patient_profiles.user_id", ondelete="RESTRICT"),
        nullable=False,
    )
    consultation_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("consultations.id", ondelete="RESTRICT")
    )
    prescriber_user_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT")
    )
    source: Mapped[PrescriptionSource] = mapped_column(
        enum_type(PrescriptionSource, "prescription_source"), nullable=False
    )
    claimed_prescriber: Mapped[str | None] = mapped_column(Text)
    review_status: Mapped[PrescriptionReviewStatus] = mapped_column(
        enum_type(PrescriptionReviewStatus, "prescription_review_status"),
        nullable=False,
        server_default=text("'not_reviewed'"),
    )
    reviewed_by: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT")
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[PrescriptionStatus] = mapped_column(
        enum_type(PrescriptionStatus, "prescription_status"),
        nullable=False,
        server_default=text("'draft'"),
    )
    issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    starts_on: Mapped[date | None] = mapped_column(Date)
    ends_on: Mapped[date | None] = mapped_column(Date)
    previous_prescription_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("prescriptions.id", ondelete="RESTRICT")
    )
    original_document_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("documents.id", ondelete="RESTRICT")
    )
    created_by: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    status_reason: Mapped[str | None] = mapped_column(Text)


class PrescriptionItem(Base):
    __tablename__ = "prescription_items"
    __table_args__ = (
        CheckConstraint(
            "duration_days IS NULL OR duration_days > 0", name=conv("ck_item_duration")
        ),
        CheckConstraint("quantity IS NULL OR quantity > 0", name=conv("ck_item_quantity")),
        CheckConstraint(
            "(quantity IS NULL) = (quantity_unit IS NULL)", name=conv("ck_item_quantity_unit")
        ),
        Index("ix_prescription_items_prescription", "prescription_id"),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    prescription_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("prescriptions.id", ondelete="RESTRICT"), nullable=False
    )
    medicine_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("medicines.id", ondelete="RESTRICT")
    )
    medicine_name_snapshot: Mapped[str] = mapped_column(Text, nullable=False)
    dose_text: Mapped[str] = mapped_column(Text, nullable=False)
    route: Mapped[str | None] = mapped_column(Text)
    frequency: Mapped[str] = mapped_column(Text, nullable=False)
    duration_days: Mapped[int | None] = mapped_column(Integer)
    instructions: Mapped[str | None] = mapped_column(Text)
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    quantity_unit: Mapped[str | None] = mapped_column(Text)
    is_prn: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
