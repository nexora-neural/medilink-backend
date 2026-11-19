from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.identity.models import User
from app.modules.organizations.models import Organization, OrganizationMembership
from app.modules.profiles.models import ProfessionalProfile


async def get_doctor_profile(
    db: AsyncSession,
    user_id: UUID,
) -> ProfessionalProfile | None:
    return await db.scalar(
        select(ProfessionalProfile).where(
            ProfessionalProfile.user_id == user_id,
            ProfessionalProfile.role == "doctor",
        )
    )


async def find_active_doctor_by_reference(
    db: AsyncSession,
    doctor_reference: str,
) -> tuple[User, ProfessionalProfile] | None:
    result = await db.execute(
        select(User, ProfessionalProfile)
        .join(ProfessionalProfile, ProfessionalProfile.user_id == User.id)
        .where(
            User.status == "active",
            ProfessionalProfile.role == "doctor",
            ProfessionalProfile.doctor_reference == doctor_reference,
        )
    )
    row = result.one_or_none()
    if row is None:
        return None

    user, profile = row
    return user, profile


async def list_approved_doctor_clinics(
    db: AsyncSession,
    user_id: UUID,
) -> list[Organization]:
    result = await db.scalars(
        select(Organization)
        .join(
            OrganizationMembership,
            OrganizationMembership.organization_id == Organization.id,
        )
        .where(
            OrganizationMembership.user_id == user_id,
            OrganizationMembership.role == "doctor",
            OrganizationMembership.status == "approved",
            Organization.status == "active",
            Organization.type == "clinic",
        )
        .order_by(Organization.name, Organization.id)
    )
    return list(result.all())