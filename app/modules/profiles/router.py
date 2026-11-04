"""API router for user profiles."""

from collections.abc import Awaitable, Callable
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.session import get_session
from app.modules.identity.auth import get_current_user
from app.modules.identity.models import User
from app.modules.profiles.models import ProfessionalProfile
from app.modules.profiles.schemas import (
    AccountUpdateRequest,
    MeResponse,
    PatientProfileResponse,
    PatientProfileUpdateRequest,
    ProfessionalProfileResponse,
)
from app.modules.profiles.service import (
    build_me_response,
    update_own_account,
    update_own_patient_profile,
)

router = APIRouter(tags=["Profiles"])

AccountAuditWriter = Callable[[AsyncSession, UUID, list[str]], Awaitable[None]]


def get_account_audit_writer() -> AccountAuditWriter:
    """Block account updates until the audit writer is available."""
    raise HTTPException(
        status_code=503,
        detail="Account audit service is unavailable.",
    )


def get_patient_profile_audit_writer() -> AccountAuditWriter:
    """Block patient profile updates until the audit writer is available."""
    raise HTTPException(
        status_code=503,
        detail="Patient profile audit service is unavailable.",
    )


@router.get("/me", response_model=MeResponse)
async def get_me(
    user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> MeResponse:
    """Return account details for the authenticated user."""
    profile = await session.get(ProfessionalProfile, user.id)

    professional_profile = None
    if profile is not None:
        professional_profile = ProfessionalProfileResponse.model_validate(
            profile,
            from_attributes=True,
        )

    return await build_me_response(
        session,
        user,
        professional_profile=professional_profile,
    )


@router.patch("/me", response_model=MeResponse)
async def patch_me(
    data: AccountUpdateRequest,
    user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    write_audit: Annotated[AccountAuditWriter, Depends(get_account_audit_writer)],
) -> MeResponse:
    """Update the authenticated user's account and record the change."""
    try:
        await update_own_account(session, user, data, write_audit=write_audit)
        response = await get_me(user=user, session=session)
        await session.commit()
    except Exception:
        await session.rollback()
        raise

    return response


@router.patch("/me/patient-profile", response_model=PatientProfileResponse)
async def patch_patient_profile(
    data: PatientProfileUpdateRequest,
    user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    write_audit: Annotated[
        AccountAuditWriter, Depends(get_patient_profile_audit_writer)
    ],
) -> PatientProfileResponse:
    """Update the authenticated patient's own profile."""
    try:
        response = await update_own_patient_profile(
            session, user, data, write_audit=write_audit
        )
        await session.commit()
    except Exception:
        await session.rollback()
        raise

    return response