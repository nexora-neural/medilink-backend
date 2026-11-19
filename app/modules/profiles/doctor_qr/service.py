from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.identity.authorization import require_role
from app.modules.identity.models import User

from . import repository
from .schemas import DoctorQrRead, ProfessionalIdentity


async def get_my_doctor_qr(db: AsyncSession, actor: User) -> DoctorQrRead:
    await require_role(db, actor, "doctor")
    profile = await repository.get_doctor_profile(db, actor.id)

    if (
        profile is None
        or profile.verification_status != "approved"
        or profile.doctor_reference is None
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Doctor QR is unavailable.",
        )

    return DoctorQrRead(
        doctor_reference=profile.doctor_reference,
        qr_payload=profile.doctor_reference,
    )


async def resolve_doctor_qr(
    db: AsyncSession,
    actor: User,
    doctor_reference: str,
) -> ProfessionalIdentity:
    await require_role(db, actor, "patient")

    doctor = await repository.find_active_doctor_by_reference(db, doctor_reference)
    if doctor is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Doctor reference was not found.",
        )

    user, profile = doctor
    identity = ProfessionalIdentity(
        user_id=user.id,
        role="doctor",
        display_name=user.display_name,
        verification_status=profile.verification_status,
    )

    if profile.verification_status == "approved":
        organizations = await repository.list_approved_doctor_clinics(db, user.id)
        if organizations:
            organization = organizations[0]
            identity.organization_id = organization.id
            identity.organization_name = organization.name

    return identity