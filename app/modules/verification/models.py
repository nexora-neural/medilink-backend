"""SQLAlchemy ORM models for professional verification."""

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    PrimaryKeyConstraint,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.schema import conv

from app.database.base import Base
from app.modules.identity.enums import professional_role_type, verification_status_type

if TYPE_CHECKING:
    from app.modules.documents.models import Document
    from app.modules.identity.models import User
    from app.modules.organizations.models import Organization


class VerificationApplication(Base):
    """Professional verification application and its current review state."""

    __tablename__ = "verification_applications"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="verification_applications_pkey"),
        CheckConstraint(
            "(reviewed_by IS NULL) = (reviewed_at IS NULL)",
            name=conv("ck_verification_review_pair"),
        ),
        Index("ix_verification_queue", "status", "submitted_at"),
        Index("ix_verification_user", "user_id", text("submitted_at DESC")),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey(
            "users.id",
            name="verification_applications_user_id_fkey",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    organization_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey(
            "organizations.id",
            name="verification_applications_organization_id_fkey",
            ondelete="RESTRICT",
        ),
    )
    role: Mapped[str] = mapped_column(professional_role_type, nullable=False)
    registration_number: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        verification_status_type,
        nullable=False,
        server_default=text("'pending'::verification_status"),
    )
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    reviewed_by: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey(
            "users.id",
            name="verification_applications_reviewed_by_fkey",
            ondelete="RESTRICT",
        ),
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_reason: Mapped[str | None] = mapped_column(Text)

    user: Mapped["User"] = relationship(foreign_keys=[user_id])
    organization: Mapped["Organization | None"] = relationship()
    decisions: Mapped[list["VerificationDecision"]] = relationship(
        back_populates="application", order_by="VerificationDecision.decided_at"
    )


class VerificationDecision(Base):
    """Append-only reviewer decision history for a verification application."""

    __tablename__ = "verification_decisions"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="verification_decisions_pkey"),
        CheckConstraint(
            "decision IN ('approved','rejected','more_info_requested','suspended')",
            name=conv("ck_verification_decision"),
        ),
        Index(
            "ix_verification_decisions_history",
            "application_id",
            text("decided_at DESC"),
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False, server_default=text("gen_random_uuid()")
    )
    application_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey(
            "verification_applications.id",
            name="verification_decisions_application_id_fkey",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    reviewer_user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey(
            "users.id",
            name="verification_decisions_reviewer_user_id_fkey",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    decision: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    application: Mapped["VerificationApplication"] = relationship(back_populates="decisions")
    reviewer: Mapped["User"] = relationship(foreign_keys=[reviewer_user_id])


class VerificationApplicationDocument(Base):
    __tablename__ = "verification_application_documents"

    application_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey(
            "verification_applications.id",
            name=("verification_application_documents_application_id_fkey"),
            ondelete="RESTRICT",
        ),
        nullable=False,
    )

    document_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey(
            "documents.id",
            name=("verification_application_documents_document_id_fkey"),
            ondelete="RESTRICT",
        ),
        nullable=False,
    )

    attached_by: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey(
            "users.id",
            name=("verification_application_documents_attached_by_fkey"),
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

    document: Mapped["Document"] = relationship("Document", back_populates="verification_links")
