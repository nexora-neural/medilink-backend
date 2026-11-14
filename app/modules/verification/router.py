"""API router for professional verification."""

from typing import Annotated

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.session import get_session
from app.modules.identity.auth import get_current_user
from app.modules.identity.models import User
from app.modules.identity.request_limits import require_authenticated_rate_limit
from app.modules.profiles.route import ProfileRoute
from app.modules.verification import service
from app.modules.verification.schemas import (
    ProfessionalProfileInput,
    ProfessionalProfileRead,
    VerificationApplicationInput,
    VerificationApplicationList,
    VerificationApplicationRead,
)

router = APIRouter(
    tags=["Verification"],
    route_class=ProfileRoute,
    dependencies=[Depends(require_authenticated_rate_limit)],
)

DB = Annotated[AsyncSession, Depends(get_session)]
Actor = Annotated[User, Depends(get_current_user)]


@router.put(
    "/me/professional-profile",
    response_model=ProfessionalProfileRead,
)
async def put_professional_profile(
    payload: ProfessionalProfileInput,
    request: Request,
    db: DB,
    actor: Actor,
) -> ProfessionalProfileRead:
    """Create or update the applicant's professional profile."""
    return await service.update_professional_profile(
        db, actor, request, payload
    )


@router.post(
    "/verification-applications",
    response_model=VerificationApplicationRead,
    status_code=status.HTTP_201_CREATED,
)
async def post_verification_application(
    payload: VerificationApplicationInput,
    request: Request,
    db: DB,
    actor: Actor,
) -> VerificationApplicationRead:
    """Submit a professional verification application."""
    return await service.submit_application(
        db, actor, request, payload
    )


@router.get(
    "/me/verification-applications",
    response_model=VerificationApplicationList,
)
async def get_my_verification_applications(
    request: Request,
    db: DB,
    actor: Actor,
) -> VerificationApplicationList:
    """List the authenticated applicant's verification applications."""
    return await service.list_own_applications(
        db, actor, request
    )
