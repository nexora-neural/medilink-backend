"""Prescription review evidence stored independently of prescription state."""

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Index, Text, text
from sqlalchemy.dialects.postgresql import ENUM
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class ReviewOutcome(StrEnum):
    REVIEWED = "reviewed"
    APPROVED = "approved"
    NEEDS_CLARIFICATION = "needs_clarification"
    NOT_ACCEPTED = "not_accepted"


class PrescriptionReview(Base):
    __tablename__ = "prescription_reviews"
    __table_args__ = (
        Index("ix_prescription_reviews_history", "prescription_id", text("reviewed_at DESC")),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    prescription_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("prescriptions.id", ondelete="RESTRICT"), nullable=False
    )
    reviewer_user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    outcome: Mapped[ReviewOutcome] = mapped_column(
        ENUM(
            ReviewOutcome, name="review_outcome", values_callable=lambda cls: [x.value for x in cls]
        ),
        nullable=False,
    )
    notes: Mapped[str | None] = mapped_column(Text)
    reviewed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
