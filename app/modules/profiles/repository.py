"""Database access methods for user profiles."""

from datetime import date
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.identity.repository import get_roles
from app.modules.organizations.models import OrganizationMembership
from app.modules.profiles.models import PatientProfile
from app.modules.profiles.schemas import PatientProfileUpdateRequest


async def get_patient_profile(
    session: AsyncSession,
    user_id: UUID,
    for_update: bool = False,
) -> PatientProfile | None:
    """Return the patient's profile, locking the row if requested."""
    query = select(PatientProfile).where(PatientProfile.user_id == user_id)

    if for_update:
        query = query.with_for_update()

    result = await session.execute(query)
    return result.scalar_one_or_none()


async def create_patient_profile(
    session: AsyncSession,
    user_id: UUID,
    date_of_birth: date,
    sex: str,
) -> PatientProfile:
    """Add a patient profile to the current transaction."""
    profile = PatientProfile(
        user_id=user_id,
        date_of_birth=date_of_birth,
        sex=sex,
    )
    session.add(profile)
    await session.flush()
    await session.refresh(profile)
    return profile


async def update_patient_profile(
    session: AsyncSession,
    profile: PatientProfile,
    data: PatientProfileUpdateRequest,
) -> PatientProfile:
    """Apply the supplied profile fields in the current transaction."""
    changes = data.model_dump(exclude_unset=True)
    editable_fields = (
        "date_of_birth",
        "sex",
        "emergency_contact_name",
        "emergency_contact_phone",
    )

    for field in editable_fields:
        if field in changes:
            setattr(profile, field, changes[field])

    if "notification_preferences" in changes:
        preferences = dict(profile.notification_preferences)
        preferences.update(changes["notification_preferences"])
        profile.notification_preferences = preferences

    await session.flush()
    await session.refresh(profile)
    return profile


async def get_user_role_codes(
    session: AsyncSession,
    user_id: UUID,
) -> list[str]:
    """Return the role codes assigned to the given user."""
    return sorted(await get_roles(session, user_id))


async def get_user_memberships(
    session: AsyncSession,
    user_id: UUID,
) -> list[OrganizationMembership]:
    """Return the organization memberships for the given user."""
    query = (
        select(OrganizationMembership)
        .where(OrganizationMembership.user_id == user_id)
        .order_by(OrganizationMembership.created_at, OrganizationMembership.id)
    )
    result = await session.scalars(query)
    return list(result.all())