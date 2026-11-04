"""SQLAlchemy ORM models for pharmacy dispensing."""

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    PrimaryKeyConstraint,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class DispensingEventsModel(Base):
    """Immutable pharmacy dispensing events and reversal history."""

    __tablename__ = "dispensing_events"

    __table_args__ = (
        PrimaryKeyConstraint("id", name="dispensing_events_pkey"),
        UniqueConstraint(
            "request_key",
            name="dispensing_events_request_key_key",
        ),
        UniqueConstraint(
            "reversal_of_id",
            name="uq_dispensing_single_reversal",
        ),
        CheckConstraint(
            "(reversal_of_id IS NULL) = (reason IS NULL)",
            name="dispensing_reversal",
        ),
        Index(
            "ix_dispensing_prescription_date",
            "prescription_id",
            text("dispensed_at DESC"),
        ),
        Index(
            "ix_dispensing_pharmacist_date",
            "pharmacist_user_id",
            text("dispensed_at DESC"),
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=False,
        server_default=text("gen_random_uuid()"),
    )

    prescription_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey(
            "prescriptions.id",
            name="dispensing_events_prescription_id_fkey",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )

    pharmacist_user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey(
            "users.id",
            name="dispensing_events_pharmacist_user_id_fkey",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )

    organization_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey(
            "organizations.id",
            name="dispensing_events_organization_id_fkey",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )

    share_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey(
            "prescription_shares.id",
            name="dispensing_events_share_id_fkey",
            ondelete="RESTRICT",
        ),
        nullable=True,
    )

    dispensed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    request_key: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )

    notes: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    reversal_of_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey(
            "dispensing_events.id",
            name="dispensing_events_reversal_of_id_fkey",
            ondelete="RESTRICT",
        ),
        nullable=True,
    )

    reason: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class DispensingItemsModel(Base):
    """Quantities dispensed for individual prescription items."""

    __tablename__ = "dispensing_items"

    __table_args__ = (
        PrimaryKeyConstraint("id", name="dispensing_items_pkey"),
        UniqueConstraint(
            "event_id",
            "prescription_item_id",
            name="uq_dispensing_event_item",
        ),
        CheckConstraint(
            "quantity > 0",
            name="dispensing_quantity",
        ),
        Index(
            "ix_dispensing_items_item",
            "prescription_item_id",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=False,
        server_default=text("gen_random_uuid()"),
    )

    event_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey(
            "dispensing_events.id",
            name="dispensing_items_event_id_fkey",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )

    prescription_item_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey(
            "prescription_items.id",
            name="dispensing_items_prescription_item_id_fkey",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )

    quantity: Mapped[Decimal] = mapped_column(
        Numeric(12, 3),
        nullable=False,
    )

    quantity_unit: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )