from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    PrimaryKeyConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class VerificationApplicationDocument(Base):
    __tablename__ = "verification_application_documents"

    application_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey(
            "verification_applications.id",
            name=(
                "verification_application_documents_"
                "application_id_fkey"
            ),
            ondelete="RESTRICT",
        ),
        nullable=False,
    )

    document_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey(
            "documents.id",
            name=(
                "verification_application_documents_"
                "document_id_fkey"
            ),
            ondelete="RESTRICT",
        ),
        nullable=False,
    )

    attached_by: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey(
            "users.id",
            name=(
                "verification_application_documents_"
                "attached_by_fkey"
            ),
            ondelete="RESTRICT",
        ),
        nullable=False,
    )

    attached_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        PrimaryKeyConstraint(
            "application_id",
            "document_id",
            name="verification_application_documents_pkey",
        ),
        Index(
            "ix_verification_application_documents_document",
            "document_id",
        ),
    )