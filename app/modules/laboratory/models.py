"""Laboratory upload requests and report persistence."""

from datetime import date, datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ENUM
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.schema import conv

from app.database.base import Base
from app.modules.clinical_records.models import RecordSource, record_source_enum


class UploadRequestStatus(StrEnum):
    ACTIVE = "active"
    USED = "used"
    EXPIRED = "expired"
    REVOKED = "revoked"


class ReportStatus(StrEnum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    WITHDRAWN = "withdrawn"


def enum_type(cls, name):
    return ENUM(cls, name=name, values_callable=lambda enum: [x.value for x in enum])


class LabUploadRequest(Base):
    __tablename__ = "lab_upload_requests"
    __table_args__ = (
        UniqueConstraint("request_token_hash"),
        Index("ix_lab_requests_lab_status_expiry", "laboratory_id", "status", "expires_at"),
        Index("ix_lab_requests_patient", "patient_user_id", text("created_at DESC")),
    )
    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    patient_user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("patient_profiles.user_id", ondelete="RESTRICT")
    )
    laboratory_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT")
    )
    request_token_hash: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[UploadRequestStatus] = mapped_column(
        enum_type(UploadRequestStatus, "upload_request_status"), server_default=text("'active'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ReportIdentity(Base):
    __tablename__ = "report_identities"
    __table_args__ = (
        UniqueConstraint("initial_upload_request_id"),
        UniqueConstraint("laboratory_id", "lab_reference", name="uq_report_identity_lab_reference"),
        Index("ix_report_identities_patient", "patient_user_id", text("created_at DESC")),
    )
    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    patient_user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("patient_profiles.user_id", ondelete="RESTRICT")
    )
    laboratory_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT")
    )
    lab_reference: Mapped[str] = mapped_column(Text)
    initial_upload_request_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("lab_upload_requests.id", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class Report(Base):
    __tablename__ = "reports"
    __table_args__ = (
        CheckConstraint(
            "(supersedes_id IS NULL) = (correction_reason IS NULL)",
            name=conv("ck_report_correction"),
        ),
        Index("ix_reports_patient_date", "patient_user_id", text("report_date DESC NULLS LAST")),
        Index("ix_reports_lab_date", "laboratory_id", text("report_date DESC NULLS LAST")),
        Index(
            "ix_reports_identity_date",
            "report_identity_id",
            text("report_date DESC"),
            postgresql_where=text("report_identity_id IS NOT NULL"),
        ),
    )
    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    patient_user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("patient_profiles.user_id", ondelete="RESTRICT")
    )
    laboratory_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT")
    )
    report_identity_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("report_identities.id", ondelete="RESTRICT")
    )
    upload_request_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("lab_upload_requests.id", ondelete="RESTRICT")
    )
    document_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("documents.id", ondelete="RESTRICT")
    )
    report_type: Mapped[str] = mapped_column(Text)
    report_date: Mapped[date | None] = mapped_column(Date)
    notes: Mapped[str | None] = mapped_column(Text)
    source: Mapped[RecordSource] = mapped_column(record_source_enum)
    uploaded_by: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT")
    )
    status: Mapped[ReportStatus] = mapped_column(
        enum_type(ReportStatus, "report_status"), server_default=text("'active'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    supersedes_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("reports.id", ondelete="RESTRICT")
    )
    correction_reason: Mapped[str | None] = mapped_column(Text)
