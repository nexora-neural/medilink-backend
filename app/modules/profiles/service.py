"""Business rules and orchestration for user profiles."""

from collections.abc import Awaitable, Callable
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.identity.models import User
from app.modules.profiles.repository import (
    get_patient_profile,
    get_user_memberships,
    get_user_role_codes,
    update_patient_profile,
)
from app.modules.profiles.schemas import (
    AccountUpdateRequest,
    MembershipResponse,
    MeResponse,
    PatientProfileResponse,
    PatientProfileUpdateRequest,
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


async def update_own_account(
    session: AsyncSession,
    user: User,
    data: AccountUpdateRequest,
    *,
    write_audit: Callable[[AsyncSession, UUID, list[str]], Awaitable[None]],
) -> None:
    """Update account details and record changed fields in the same session."""
    supplied_fields = data.model_dump(exclude_unset=True)
    changed_fields: list[str] = []

    for field in ("display_name", "phone"):
        if field not in supplied_fields:
            continue

        value = supplied_fields[field]
        if getattr(user, field) != value:
            setattr(user, field, value)
            changed_fields.append(field)

    if changed_fields:
        await write_audit(session, user.id, changed_fields)
        await session.flush()

async def update_own_patient_profile(
    session: AsyncSession,
    user: User,
    data: PatientProfileUpdateRequest,
    *,
    write_audit: Callable[[AsyncSession, UUID, list[str]], Awaitable[None]],
) -> PatientProfileResponse:
    """Update the patient profile owned by the authenticated user."""
    roles = await get_user_role_codes(session, user.id)
    if "patient" not in roles:
        raise HTTPException(status_code=403, detail="Patient role required.")

    profile = await get_patient_profile(session, user.id, for_update=True)
    if profile is None:
        raise HTTPException(status_code=404, detail="Patient profile not found.")

    changes = data.model_dump(exclude_unset=True)
    changed_fields: list[str] = []

    for field, value in changes.items():
        current_value = getattr(profile, field)
        if field == "notification_preferences":
            value = {**current_value, **value}

        if current_value != value:
            changed_fields.append(field)

    if changed_fields:
        profile = await update_patient_profile(session, profile, data)
        await write_audit(session, user.id, changed_fields)

    return PatientProfileResponse.model_validate(profile, from_attributes=True)