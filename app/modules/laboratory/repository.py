"""Database queries for patient lab upload requests."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.identity.models import User
from app.modules.laboratory.models import LabUploadRequest, UploadRequestStatus
from app.modules.organizations.models import Organization
from app.modules.profiles.models import PatientProfile


async def patient_profile_exists(db: AsyncSession, patient_id: UUID) -> bool:
    return await db.get(PatientProfile, patient_id) is not None


async def get_laboratory(
    db: AsyncSession,
    laboratory_id: UUID,
) -> Organization | None:
    return await db.scalar(
        select(Organization).where(
            Organization.id == laboratory_id,
            Organization.type == "laboratory",
        )
    )


async def list_patient_requests(
    db: AsyncSession,
    patient_id: UUID,
    *,
    page: int,
    page_size: int,
) -> tuple[list[LabUploadRequest], int]:
    owned_by_patient = LabUploadRequest.patient_user_id == patient_id
    total = await db.scalar(
        select(func.count()).select_from(LabUploadRequest).where(owned_by_patient)
    ) or 0

    offset = (page - 1) * page_size
    if offset >= total:
        return [], total

    rows = await db.scalars(
        select(LabUploadRequest)
        .where(owned_by_patient)
        .order_by(LabUploadRequest.created_at.desc(), LabUploadRequest.id.desc())
        .offset(offset)
        .limit(page_size)
    )
    return list(rows.all()), total


async def lock_patient_request(
    db: AsyncSession,
    request_id: UUID,
    patient_id: UUID,
) -> LabUploadRequest | None:
    return await db.scalar(
        select(LabUploadRequest)
        .where(
            LabUploadRequest.id == request_id,
            LabUploadRequest.patient_user_id == patient_id,
        )
        .with_for_update()
    )


async def find_resolvable_request(
    db: AsyncSession,
    *,
    laboratory_id: UUID,
    token_hash: str,
    now: datetime,
):
    statement = (
        select(
            LabUploadRequest.id.label("request_id"),
            LabUploadRequest.patient_user_id,
            LabUploadRequest.laboratory_id,
            LabUploadRequest.expires_at,
            User.display_name,
            PatientProfile.date_of_birth,
        )
        .join(PatientProfile, PatientProfile.user_id == LabUploadRequest.patient_user_id)
        .join(User, User.id == PatientProfile.user_id)
        .join(Organization, Organization.id == LabUploadRequest.laboratory_id)
        .where(
            LabUploadRequest.laboratory_id == laboratory_id,
            LabUploadRequest.request_token_hash == token_hash,
            LabUploadRequest.status == UploadRequestStatus.ACTIVE,
            LabUploadRequest.expires_at > now,
            LabUploadRequest.used_at.is_(None),
            Organization.type == "laboratory",
            Organization.status == "active",
            User.status == "active",
        )
    )
    return (await db.execute(statement)).mappings().one_or_none()