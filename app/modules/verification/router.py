"""API router for professional verification."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.exceptions import RequestValidationError
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
    VerificationApplicationRead,
    VerificationApplicationsReadPage,
    VerificationDecisionInput,
    VerificationDecisionsReadPage,
)

router = APIRouter(
    tags=["Verification"],
    route_class=ProfileRoute,
    dependencies=[Depends(require_authenticated_rate_limit)],
)
DB = Annotated[AsyncSession, Depends(get_session)]
Actor = Annotated[User, Depends(get_current_user)]


def _reject_unknown_query_params(request: Request, allowed: set[str]) -> None:
    unknown = sorted(set(request.query_params.keys()) - allowed)
    if unknown:
        raise RequestValidationError(
            [
                {
                    "type": "extra_forbidden",
                    "loc": ("query", name),
                    "msg": "Extra inputs are not permitted",
                    "input": None,
                }
                for name in unknown
            ]
        )


@router.put("/me/professional-profile", response_model=ProfessionalProfileRead)
async def put_professional_profile(
    payload: ProfessionalProfileInput, request: Request, db: DB, actor: Actor
) -> ProfessionalProfileRead:
    return await service.update_professional_profile(db, actor, request, payload)


@router.post(
    "/verification-applications",
    response_model=VerificationApplicationRead,
    status_code=status.HTTP_201_CREATED,
)
async def post_verification_application(
    payload: VerificationApplicationInput, request: Request, db: DB, actor: Actor
) -> VerificationApplicationRead:
    return await service.submit_application(db, actor, request, payload)


@router.get("/me/verification-applications", response_model=VerificationApplicationsReadPage)
async def get_my_verification_applications(
    request: Request,
    db: DB,
    actor: Actor,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> VerificationApplicationsReadPage:
    return await service.list_own_applications(db, actor, request, page=page, page_size=page_size)


@router.get("/admin/verification-applications", response_model=VerificationApplicationsReadPage)
async def get_verification_review_queue(
    request: Request,
    db: DB,
    actor: Actor,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    status_filter: str | None = Query(default=None, alias="status", max_length=200),
    role_filter: str | None = Query(default=None, alias="role", max_length=200),
) -> VerificationApplicationsReadPage:
    _reject_unknown_query_params(request, {"page", "page_size", "status", "role"})
    return await service.list_review_applications(
        db,
        actor,
        request,
        page=page,
        page_size=page_size,
        status=status_filter,
        role=role_filter,
    )


@router.get(
    "/admin/verification-applications/{application_id}",
    response_model=VerificationApplicationRead,
)
async def get_verification_application(
    application_id: UUID, request: Request, db: DB, actor: Actor
) -> VerificationApplicationRead:
    _reject_unknown_query_params(request, set())
    return await service.get_review_application(db, actor, request, application_id)


@router.post(
    "/admin/verification-applications/{application_id}/decision",
    response_model=VerificationApplicationRead,
    status_code=status.HTTP_201_CREATED,
)
async def post_verification_decision(
    application_id: UUID,
    payload: VerificationDecisionInput,
    request: Request,
    db: DB,
    actor: Actor,
) -> VerificationApplicationRead:
    _reject_unknown_query_params(request, set())
    return await service.decide_application(db, actor, request, application_id, payload)


@router.get(
    "/admin/verification-applications/{application_id}/decisions",
    response_model=VerificationDecisionsReadPage,
)
async def get_verification_decision_history(
    application_id: UUID,
    request: Request,
    db: DB,
    actor: Actor,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> VerificationDecisionsReadPage:
    _reject_unknown_query_params(request, {"page", "page_size"})
    return await service.list_application_decisions(
        db, actor, request, application_id, page=page, page_size=page_size
    )
