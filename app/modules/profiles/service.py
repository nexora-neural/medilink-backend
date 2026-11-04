"""Business rules and orchestration for user profiles."""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.identity.models import User
from app.modules.profiles.repository import (
    get_patient_profile,
    get_user_memberships,
    get_user_role_codes,
)
from app.modules.profiles.schemas import (
    MembershipResponse,
    MeResponse,
    PatientProfileResponse,
    ProfessionalProfileResponse,
    UserResponse,
)


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


async def build_me_response(
    session: AsyncSession,
    user: User,
    *,
    professional_profile: ProfessionalProfileResponse | None,
) -> MeResponse:
    """Build the account response for the authenticated user."""
    roles = await get_user_role_codes(session, user.id)
    patient_profile = await read_own_patient_profile(session, user.id)
    memberships = await get_user_memberships(session, user.id)

    return MeResponse(
        user=UserResponse.model_validate(user, from_attributes=True),
        roles=roles,
        patient_profile=patient_profile,
        professional_profile=professional_profile,
        memberships=[
            MembershipResponse.model_validate(membership, from_attributes=True)
            for membership in memberships
        ],
    )