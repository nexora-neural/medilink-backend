"""Read-only database queries for limited FHIR exports."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.consent.models import AccessGrant, GrantCategory
from app.modules.identity.models import Role, User, UserRole
from app.modules.prescriptions.core_models import Prescription, PrescriptionItem
from app.modules.profiles.models import PatientProfile, ProfessionalProfile


async def get_patient(
    session: AsyncSession, patient_id: UUID
) -> tuple[User, PatientProfile] | None:
    row = await session.execute(
        select(User, PatientProfile)
        .join(PatientProfile, PatientProfile.user_id == User.id)
        .where(User.id == patient_id)
    )
    return row.one_or_none()


async def get_medication_item(
    session: AsyncSession, item_id: UUID
) -> tuple[PrescriptionItem, Prescription, User, PatientProfile] | None:
    row = await session.execute(
        select(PrescriptionItem, Prescription, User, PatientProfile)
        .join(Prescription, Prescription.id == PrescriptionItem.prescription_id)
        .join(User, User.id == Prescription.patient_user_id)
        .join(PatientProfile, PatientProfile.user_id == User.id)
        .where(PrescriptionItem.id == item_id)
    )
    return row.one_or_none()


async def get_active_role_codes(session: AsyncSession, user_id: UUID) -> set[str]:
    result = await session.scalars(
        select(Role.code)
        .join(UserRole, UserRole.role_id == Role.id)
        .where(UserRole.user_id == user_id, UserRole.revoked_at.is_(None))
    )
    return set(result.all())


async def get_current_grant_organizations(
    session: AsyncSession,
    *,
    patient_id: UUID,
    doctor_id: UUID,
    category: str,
) -> list[UUID | None]:
    """Return org scopes from grants that are active right now for this category."""
    now = datetime.now(UTC)
    result = await session.scalars(
        select(AccessGrant.organization_id)
        .join(GrantCategory, GrantCategory.grant_id == AccessGrant.id)
        .where(
            AccessGrant.patient_user_id == patient_id,
            AccessGrant.professional_user_id == doctor_id,
            AccessGrant.status == "active",
            AccessGrant.revoked_at.is_(None),
            AccessGrant.starts_at <= now,
            AccessGrant.expires_at > now,
            GrantCategory.category == category,
        )
        .distinct()
    )
    return list(result.all())


async def is_verified_doctor(session: AsyncSession, user_id: UUID) -> bool:
    return (
        await session.scalar(
            select(ProfessionalProfile.user_id).where(
                ProfessionalProfile.user_id == user_id,
                ProfessionalProfile.role == "doctor",
                ProfessionalProfile.verification_status == "approved",
            )
        )
        is not None
    )
