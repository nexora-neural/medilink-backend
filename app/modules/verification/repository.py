"""Database access methods for professional verification."""

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.documents.models import Document
from app.modules.identity.models import Role, UserRole
from app.modules.profiles.models import ProfessionalProfile
from app.modules.verification.models import (
    VerificationApplication,
    VerificationApplicationDocument,
)


async def get_professional_profile(
    db: AsyncSession, user_id: UUID, *, lock: bool = False
) -> ProfessionalProfile | None:
    statement = select(ProfessionalProfile).where(ProfessionalProfile.user_id == user_id)
    if lock:
        statement = statement.with_for_update()
    return await db.scalar(statement)


async def get_assigned_professional_roles(db: AsyncSession, user_id: UUID) -> set[str]:
    rows = await db.scalars(
        select(Role.code)
        .join(UserRole, UserRole.role_id == Role.id)
        .where(
            UserRole.user_id == user_id,
            UserRole.revoked_at.is_(None),
            Role.code.in_(("doctor", "pharmacist", "lab_staff")),
        )
    )
    return set(rows.all())


async def get_user_applications(db: AsyncSession, user_id: UUID) -> list[VerificationApplication]:
    result = await db.scalars(
        select(VerificationApplication)
        .where(VerificationApplication.user_id == user_id)
        .order_by(VerificationApplication.submitted_at.desc(), VerificationApplication.id.desc())
    )
    return list(result.all())


async def get_user_applications_page(
    db: AsyncSession, user_id: UUID, *, page: int, page_size: int
) -> tuple[list[VerificationApplication], int]:
    total = await db.scalar(
        select(func.count(VerificationApplication.id)).where(
            VerificationApplication.user_id == user_id
        )
    )
    result = await db.scalars(
        select(VerificationApplication)
        .where(VerificationApplication.user_id == user_id)
        .order_by(VerificationApplication.submitted_at.desc(), VerificationApplication.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return list(result.all()), int(total or 0)


async def get_application_documents(db: AsyncSession, application_id: UUID) -> list[UUID]:
    result = await db.scalars(
        select(VerificationApplicationDocument.document_id)
        .where(VerificationApplicationDocument.application_id == application_id)
        .order_by(
            VerificationApplicationDocument.attached_at,
            VerificationApplicationDocument.document_id,
        )
    )
    return list(result.all())


async def get_owned_evidence(db: AsyncSession, document_id: UUID, user_id: UUID) -> Document | None:
    return await db.scalar(
        select(Document)
        .where(
            Document.id == document_id,
            Document.owner_user_id == user_id,
            Document.uploaded_by == user_id,
            Document.patient_user_id.is_(None),
            Document.document_type == "verification_evidence",
            Document.status == "active",
        )
        .with_for_update()
    )
