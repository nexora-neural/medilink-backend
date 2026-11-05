"""Business rules and orchestration for user profiles."""

from collections.abc import Awaitable, Callable
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.identity.models import Role, User, UserRole
from app.modules.profiles.models import PatientProfile, ProfessionalProfile
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
    OnboardRequest,
    PatientProfileResponse,
    PatientProfileUpdateRequest,
    ProfessionalProfileResponse,
    UserResponse,
)


async def onboard_identity(session: AsyncSession, identity: dict, data: OnboardRequest):
    """Reconcile by verified Firebase UID without merging accounts by email."""
    uid = identity["uid"]
    email = identity.get("email")
    if email is not None and not isinstance(email, str):
        raise HTTPException(status_code=400, detail="Invalid verified identity.")
    result = await session.execute(
        insert(User)
        .values(
            firebase_uid=uid,
            email=email,
            display_name=data.display_name,
            phone=data.phone,
            status="active",
        )
        .on_conflict_do_nothing(index_elements=[User.firebase_uid])
        .returning(User.id)
    )
    created = result.scalar_one_or_none()
    user = await session.scalar(
        select(User).where(User.firebase_uid == uid).with_for_update()
    )
    if user.status in {"suspended", "disabled"}:
        raise HTTPException(status_code=403, detail="The local account is inactive.")
    role = await session.scalar(select(Role).where(Role.code == data.requested_role))
    if role is None:
        raise HTTPException(status_code=503, detail="Required role is not configured.")
    await session.execute(
        insert(UserRole)
        .values(user_id=user.id, role_id=role.id)
        .on_conflict_do_nothing(index_elements=[UserRole.user_id, UserRole.role_id])
    )
    if data.requested_role == "patient":
        await session.execute(
            insert(PatientProfile)
            .values(user_id=user.id, date_of_birth=data.date_of_birth, sex=data.sex)
            .on_conflict_do_nothing(index_elements=[PatientProfile.user_id])
        )
    else:
        profile = await session.get(ProfessionalProfile, user.id)
        if profile is not None and profile.role != data.requested_role:
            raise HTTPException(
                status_code=409, detail="Professional role already exists."
            )
        if profile is None:
            session.add(
                ProfessionalProfile(
                    user_id=user.id,
                    role=data.requested_role,
                    doctor_reference=uuid4().hex
                    if data.requested_role == "doctor"
                    else None,
                )
            )
    await session.flush()
    profile = await session.get(ProfessionalProfile, user.id)
    professional = (
        ProfessionalProfileResponse.model_validate(profile, from_attributes=True)
        if profile is not None
        else None
    )
    return (
        user,
        created is not None,
        await build_me_response(session, user, professional_profile=professional),
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
        await session.refresh(user)


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
