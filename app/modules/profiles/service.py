"""Business rules and orchestration for user profiles."""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.profiles.repository import get_patient_profile
from app.modules.profiles.schemas import PatientProfileResponse


async def read_own_patient_profile(
    session: AsyncSession,
    authenticated_user_id: UUID,
) -> PatientProfileResponse | None:
    """Return the patient profile for the authenticated account."""
    profile = await get_patient_profile(session, authenticated_user_id)

    if profile is None:
        return None

    return PatientProfileResponse.model_validate(
        profile,
        from_attributes=True,
    )