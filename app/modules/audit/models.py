"""Operational records, scoped idempotency and audit evidence."""

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import (
    CHAR,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ENUM, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.schema import conv

from app.database.base import Base


class IdempotencyActorType(StrEnum):
    USER = "user"
    CLIENT = "client"


class JobRun(Base):
    __tablename__ = "job_runs"
    __table_args__ = (
        UniqueConstraint("job_key"),
        CheckConstraint(
            "status IN ('queued','running','succeeded','failed','cancelled')",
            name=conv("ck_job_run_status"),
        ),
        CheckConstraint("attempt_count >= 0", name=conv("ck_job_attempts")),
        Index("ix_job_runs_status_created", "status", text("created_at DESC")),
    )
    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    job_key: Mapped[str] = mapped_column(Text)
    job_type: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    attempt_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class BackupRun(Base):
    __tablename__ = "backup_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running','succeeded','failed')", name=conv("ck_backup_status")
        ),
        CheckConstraint(
            "restore_check_status IN ('not_run','passed','failed')",
            name=conv("ck_backup_restore_check"),
        ),
        Index("ix_backup_runs_status_started", "status", text("started_at DESC")),
    )
    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    status: Mapped[str] = mapped_column(Text)
    database_included: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    files_included: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    restore_check_status: Mapped[str] = mapped_column(Text, server_default=text("'not_run'"))
    restore_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(Text)


class ApiIdempotencyRecord(Base):
    __tablename__ = "api_idempotency_records"
    __table_args__ = (
        CheckConstraint("request_digest ~ '^[0-9a-fA-F]{64}$'", name=conv("ck_idempotency_digest")),
        CheckConstraint("response_status BETWEEN 200 AND 299", name=conv("ck_idempotency_status")),
        UniqueConstraint(
            "actor_type",
            "actor_id",
            "operation_key",
            "target_key",
            "idempotency_key",
            name="uq_idempotency_scoped_key",
        ),
        Index("ix_idempotency_expiry", "expires_at"),
    )
    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    actor_type: Mapped[IdempotencyActorType] = mapped_column(
        ENUM(
            IdempotencyActorType,
            name="idempotency_actor_type",
            values_callable=lambda enum: [x.value for x in enum],
        )
    )
    actor_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    operation_key: Mapped[str] = mapped_column(Text)
    target_key: Mapped[str] = mapped_column(Text, server_default=text("'-'"))
    idempotency_key: Mapped[str] = mapped_column(Text)
    request_digest: Mapped[str] = mapped_column(CHAR(64))
    response_status: Mapped[int] = mapped_column(SmallInteger)
    response_body: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (
        CheckConstraint(
            "num_nonnulls(actor_user_id, actor_client_id) <= 1", name=conv("ck_audit_actor")
        ),
        Index("ix_audit_target_time", "target_type", "target_id", text("created_at DESC")),
        Index(
            "ix_audit_actor_time",
            "actor_user_id",
            text("created_at DESC"),
            postgresql_where=text("actor_user_id IS NOT NULL"),
        ),
        Index("ix_audit_request", "request_id"),
    )
    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    actor_user_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT")
    )
    actor_client_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("integration_clients.id", ondelete="RESTRICT")
    )
    action: Mapped[str] = mapped_column(Text)
    target_type: Mapped[str] = mapped_column(Text)
    target_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    outcome: Mapped[str] = mapped_column(Text)
    request_id: Mapped[str] = mapped_column(Text)
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
