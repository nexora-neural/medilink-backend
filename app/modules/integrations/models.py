"""Integration client and submission persistence models."""

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import ARRAY, ENUM
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.schema import conv

from app.database.base import Base


class IntegrationStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    SUSPENDED = "suspended"
    REVOKED = "revoked"
    EXPIRED = "expired"


class SubmissionStatus(StrEnum):
    RECEIVED = "received"
    PROCESSING = "processing"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    FAILED = "failed"


def enum_type(cls, name):
    return ENUM(cls, name=name, values_callable=lambda enum: [x.value for x in enum])


class IntegrationClient(Base):
    __tablename__ = "integration_clients"
    __table_args__ = (
        UniqueConstraint("credential_hash"),
        CheckConstraint(
            "scopes <@ ARRAY['reports:write']::text[]", name=conv("ck_integration_scope")
        ),
        CheckConstraint(
            "(status = 'revoked') = (revoked_at IS NOT NULL)", name=conv("ck_integration_revoked")
        ),
        Index("ix_integration_clients_org_status", "organization_id", "status", "expires_at"),
    )
    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    organization_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT")
    )
    name: Mapped[str] = mapped_column(Text)
    credential_hash: Mapped[str] = mapped_column(Text)
    scopes: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("ARRAY[]::text[]"))
    status: Mapped[IntegrationStatus] = mapped_column(
        enum_type(IntegrationStatus, "integration_status"), server_default=text("'pending'")
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class IntegrationSubmission(Base):
    __tablename__ = "integration_submissions"
    __table_args__ = (
        UniqueConstraint("client_id", "idempotency_key", name="uq_integration_idempotency"),
        Index("ix_integration_submissions_status", "status", "received_at"),
        Index("uq_integration_external_report", "client_id", "external_report_id", unique=True),
    )
    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    client_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("integration_clients.id", ondelete="RESTRICT")
    )
    upload_request_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("lab_upload_requests.id", ondelete="RESTRICT")
    )
    external_report_id: Mapped[str] = mapped_column(Text)
    idempotency_key: Mapped[str] = mapped_column(Text)
    status: Mapped[SubmissionStatus] = mapped_column(
        enum_type(SubmissionStatus, "submission_status"), server_default=text("'received'")
    )
    report_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("reports.id", ondelete="RESTRICT")
    )
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    error_code: Mapped[str | None] = mapped_column(Text)
