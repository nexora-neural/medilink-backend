"""Database access methods for user profiles."""

from datetime import date
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.profiles.models import PatientProfile
from app.modules.profiles.schemas import PatientProfileUpdateRequest


async def get_patient_profile(
    session: AsyncSession,
    user_id: UUID,
    for_update: bool = False,
):
    query = select(PatientProfile).where(
        PatientProfile.user_id == user_id
    )

    if for_update:
        query = query.with_for_update()

    result = await session.execute(query)
    return result.scalar_one_or_none()


async def create_patient_profile(
    session: AsyncSession,
    user_id: UUID,
    date_of_birth: date,
    sex: str,
):
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
):
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