"""SQLAlchemy models for OCR jobs and doctor-owned prescription templates."""

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    PrimaryKeyConstraint,
    SmallInteger,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.schema import conv

from app.database.base import Base
from app.modules.prescriptions.enums import (
    PrescriptionOcrJobStatus,
    PrescriptionTemplateStatus,
    PrescriptionTemplateType,
    prescription_ocr_job_status_type,
    prescription_template_status_type,
    prescription_template_type,
)


class PrescriptionOcrJobModel(Base):
    """Retained OCR processing metadata for an original patient document."""

    __tablename__ = "prescription_ocr_jobs"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="prescription_ocr_jobs_pkey"),
        UniqueConstraint(
            "patient_user_id", "original_document_id", name="uq_prescription_ocr_patient_document"
        ),
        UniqueConstraint(
            "resulting_prescription_id",
            name="prescription_ocr_jobs_resulting_prescription_id_key",
        ),
        CheckConstraint("attempt_count >= 0", name=conv("ck_prescription_ocr_attempts")),
        CheckConstraint(
            "extracted_fields IS NULL OR jsonb_typeof(extracted_fields) = 'object'",
            name=conv("ck_prescription_ocr_extracted_object"),
        ),
        CheckConstraint(
            "(resulting_prescription_id IS NULL) = (status <> 'draft_created')",
            name=conv("ck_prescription_ocr_draft_state"),
        ),
        Index("ix_prescription_ocr_patient_created", "patient_user_id", text("created_at DESC")),
        Index("ix_prescription_ocr_status_created", "status", text("created_at DESC")),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False, server_default=text("gen_random_uuid()")
    )
    patient_user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("patient_profiles.user_id", ondelete="RESTRICT"),
        nullable=False,
    )
    original_document_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("documents.id", ondelete="RESTRICT"), nullable=False
    )
    requested_by_user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[PrescriptionOcrJobStatus] = mapped_column(
        prescription_ocr_job_status_type, nullable=False, server_default=text("'queued'")
    )
    # Python None must be stored as SQL NULL to satisfy the JSON object check;
    # JSONB's default behavior would serialize it as the JSON value `null`.
    extracted_fields: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    error_code: Mapped[str | None] = mapped_column(Text)
    resulting_prescription_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("prescriptions.id", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class PrescriptionTemplateModel(Base):
    """Reusable medication entries owned by one doctor."""

    __tablename__ = "prescription_templates"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="prescription_templates_pkey"),
        CheckConstraint(
            "(status = 'archived') = (archived_at IS NOT NULL)",
            name=conv("ck_prescription_template_archive"),
        ),
        CheckConstraint(
            "length(btrim(name)) BETWEEN 1 AND 120",
            name=conv("ck_prescription_template_name"),
        ),
        Index(
            "uq_prescription_template_active_name",
            "doctor_user_id",
            text("lower(name)"),
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
        Index(
            "ix_prescription_templates_owner_status",
            "doctor_user_id",
            "status",
            text("created_at DESC"),
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False, server_default=text("gen_random_uuid()")
    )
    doctor_user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    template_type: Mapped[PrescriptionTemplateType] = mapped_column(
        prescription_template_type, nullable=False
    )
    status: Mapped[PrescriptionTemplateStatus] = mapped_column(
        prescription_template_status_type, nullable=False, server_default=text("'active'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PrescriptionTemplateItemModel(Base):
    """A medication item snapshot belonging to a reusable doctor template."""

    __tablename__ = "prescription_template_items"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="prescription_template_items_pkey"),
        UniqueConstraint(
            "template_id", "position", name="uq_prescription_template_item_position"
        ),
        CheckConstraint("position > 0", name=conv("ck_prescription_template_item_position")),
        CheckConstraint(
            "duration_days IS NULL OR duration_days > 0",
            name=conv("ck_prescription_template_item_duration"),
        ),
        CheckConstraint(
            "quantity IS NULL OR quantity > 0",
            name=conv("ck_prescription_template_item_quantity"),
        ),
        CheckConstraint(
            "(quantity IS NULL) = (quantity_unit IS NULL)",
            name=conv("ck_prescription_template_item_quantity_unit"),
        ),
        Index("ix_prescription_template_items_template", "template_id", "position"),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False, server_default=text("gen_random_uuid()")
    )
    template_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("prescription_templates.id", ondelete="RESTRICT"),
        nullable=False,
    )
    position: Mapped[int] = mapped_column(SmallInteger, nullable=False)
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
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
