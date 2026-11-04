"""SQLAlchemy ORM models for prescriptions and OCR workflow."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    PrimaryKeyConstraint,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base
from app.modules.prescriptions.enums import ShareStatus, share_status_type


class PrescriptionSharesModel(Base):
    """One-time prescription sharing claims and confirmed access."""

    __tablename__ = "prescription_shares"

    __table_args__ = (
        PrimaryKeyConstraint("id", name="prescription_shares_pkey"),
        UniqueConstraint(
            "claim_token_hash",
            name="prescription_shares_claim_token_hash_key",
        ),
        CheckConstraint(
            "(claimed_by IS NULL) = (claimed_at IS NULL)",
            name="share_claim_pair",
        ),
        CheckConstraint(
            "(claimed_by IS NULL) = (claimed_organization_id IS NULL)",
            name="share_claim_org",
        ),
        CheckConstraint(
            "status <> 'confirmed' OR "
            "(confirmed_at IS NOT NULL AND claimed_by IS NOT NULL)",
            name="share_confirm",
        ),
        CheckConstraint(
            "(confirmed_at IS NULL) = "
            "(confirmed_access_expires_at IS NULL)",
            name="share_access_expiry",
        ),
        CheckConstraint(
            "(status = 'revoked') = (revoked_at IS NOT NULL)",
            name="share_revoked",
        ),
        Index(
            "ix_prescription_shares_expiry",
            "status",
            "claim_expires_at",
            "confirmed_access_expires_at",
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
            name="prescription_shares_prescription_id_fkey",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )

    patient_user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey(
            "patient_profiles.user_id",
            name="prescription_shares_patient_user_id_fkey",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )

    claim_token_hash: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )

    status: Mapped[ShareStatus] = mapped_column(
        share_status_type,
        nullable=False,
        server_default=text("'created'"),
    )

    claim_expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )

    claimed_by: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey(
            "users.id",
            name="prescription_shares_claimed_by_fkey",
            ondelete="RESTRICT",
        ),
        nullable=True,
    )

    claimed_organization_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey(
            "organizations.id",
            name="prescription_shares_claimed_organization_id_fkey",
            ondelete="RESTRICT",
        ),
        nullable=True,
    )

    claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    confirmed_access_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )