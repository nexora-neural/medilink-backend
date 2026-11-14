"""Database access methods for professional verification."""



from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.documents.models import Document
from app.modules.profiles.models import ProfessionalProfile
from app.modules.verification.models import (
    VerificationApplication,
    VerificationApplicationDocument,
)


async def get_professional_profile(
    db: AsyncSession,
    user_id: UUID,
    *,
    lock: bool = False,
) -> ProfessionalProfile | None:
    statement = select(ProfessionalProfile).where(
        ProfessionalProfile.user_id == user_id
    )

    if lock:
        statement = statement.with_for_update()

    return await db.scalar(statement)


async def get_user_applications(
    db: AsyncSession,
    user_id: UUID,
) -> list[VerificationApplication]:
    result = await db.scalars(
        select(VerificationApplication)
        .where(VerificationApplication.user_id == user_id)
        .order_by(
            VerificationApplication.submitted_at.desc(),
            VerificationApplication.id.desc(),
        )
    )
    return list(result.all())


async def get_application_documents(
    db: AsyncSession,
    application_id: UUID,
) -> list[UUID]:
    result = await db.scalars(
        select(VerificationApplicationDocument.document_id)
        .where(
            VerificationApplicationDocument.application_id == application_id
        )
        .order_by(
            VerificationApplicationDocument.attached_at,
            VerificationApplicationDocument.document_id,
        )
    )
    return list(result.all())


async def get_owned_evidence(
    db: AsyncSession,
    document_id: UUID,
    user_id: UUID,
) -> Document | None:
    return await db.scalar(
        select(Document)
        .where(
            Document.id == document_id,
            Document.owner_user_id == user_id,
            Document.uploaded_by == user_id,
            Document.document_type == "verification_evidence",
            Document.status == "active",
        )
        .with_for_update()
    )
