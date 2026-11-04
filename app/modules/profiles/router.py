"""API router for user profiles."""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.session import get_session
from app.modules.identity.auth import get_current_user
from app.modules.identity.models import User
from app.modules.profiles.models import ProfessionalProfile
from app.modules.profiles.schemas import MeResponse, ProfessionalProfileResponse
from app.modules.profiles.service import build_me_response

router = APIRouter(tags=["Profiles"])


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