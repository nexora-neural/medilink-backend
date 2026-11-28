"""Private, retained prescription action reasons and version lineage."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, Text, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class PrescriptionLifecycleEvent(Base):
    __tablename__ = "prescription_lifecycle_events"
    __table_args__ = (
        CheckConstraint(
            "action IN ('version','renewal','reissue','cancel','discontinue',"
            "'complete','superseded')",
            name="prescription_lifecycle_action",
        ),
        CheckConstraint(
            "length(btrim(reason)) BETWEEN 1 AND 1000", name="prescription_lifecycle_reason"
        ),
        CheckConstraint(
            "previous_version >= 0 AND new_version > previous_version",
            name="prescription_lifecycle_version",
        ),
        Index("ix_prescription_lifecycle_history", "prescription_id", "created_at", "id"),
    )
    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    prescription_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("prescriptions.id", ondelete="RESTRICT"), nullable=False
    )
    related_prescription_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("prescriptions.id", ondelete="RESTRICT")
    )
    actor_user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    action: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    previous_status: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    previous_version: Mapped[int] = mapped_column(Integer, nullable=False)
    new_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
