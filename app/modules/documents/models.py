from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CHAR,
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
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.schema import conv

from app.database.base import Base
from app.modules.documents.enum import (
    DocumentStatus,
    RecordSource,
    document_status_type,
    record_source_type,
)

if TYPE_CHECKING:
    from app.modules.identity.models import User
    from app.modules.profiles.models import PatientProfile
    from app.modules.verification.models import (
        VerificationApplicationDocument,
    )


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=False,
        server_default=text("gen_random_uuid()"),
    )

    owner_user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey(
            "users.id",
            name="documents_owner_user_id_fkey",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )

    patient_user_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey(
            "patient_profiles.user_id",
            name="documents_patient_user_id_fkey",
            ondelete="RESTRICT",
        ),
        nullable=True,
    )

    uploaded_by: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey(
            "users.id",
            name="documents_uploaded_by_fkey",
            ondelete="RESTRICT",
        ),
        nullable=True,
    )

    source: Mapped[RecordSource] = mapped_column(
        record_source_type,
        nullable=False,
    )

    document_type: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )

    original_filename: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )

    storage_key: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )

    media_type: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )

    byte_size: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
    )

    sha256: Mapped[str | None] = mapped_column(
        CHAR(64),
        nullable=True,
    )

    status: Mapped[DocumentStatus] = mapped_column(
        document_status_type,
        nullable=False,
        server_default=text("'active'"),
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    supersedes_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey(
            "documents.id",
            name="documents_supersedes_id_fkey",
            ondelete="RESTRICT",
        ),
        nullable=True,
    )


    owner: Mapped["User"] = relationship(
        "User",
        foreign_keys=[owner_user_id],
        lazy="raise",
    )


    patient: Mapped["PatientProfile | None"] = relationship(
        "PatientProfile",
        foreign_keys=[patient_user_id],
        lazy="raise",
    )

    uploader: Mapped["User | None"] = relationship(
        "User",
        foreign_keys=[uploaded_by],
        lazy="raise",
    )

    supersedes: Mapped["Document | None"] = relationship(
        "Document",
        foreign_keys=[supersedes_id],
        remote_side=[id],
        back_populates="replacement_documents",
        lazy="raise",
    )


    replacement_documents: Mapped[list["Document"]] = relationship(
        "Document",
        foreign_keys=[supersedes_id],
        back_populates="supersedes",
        passive_deletes="all",
        lazy="raise",
    )

    verification_links: Mapped[
        list["VerificationApplicationDocument"]
    ] = relationship(
        "VerificationApplicationDocument",
        back_populates="document",
        passive_deletes="all",
        lazy="raise",
    )

    __table_args__ = (
        PrimaryKeyConstraint(
            "id",
            name="documents_pkey",
        ),
        UniqueConstraint(
            "storage_key",
            name="documents_storage_key_key",
        ),
        CheckConstraint(
            "byte_size > 0 AND byte_size <= 10485760",
            name=conv("ck_document_size"),
        ),
        CheckConstraint(
            "media_type IN "
            "('application/pdf', 'image/jpeg', 'image/png')",
            name=conv("ck_document_media_type"),
        ),
        CheckConstraint(
            "sha256 IS NULL OR sha256 ~ '^[0-9a-fA-F]{64}$'",
            name=conv("ck_document_sha"),
        ),
    )


Index(
    "ix_documents_patient_date",
    Document.patient_user_id,
    Document.created_at.desc(),
)

Index(
    "ix_documents_owner_date",
    Document.owner_user_id,
    Document.created_at.desc(),
)