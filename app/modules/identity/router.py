"""Account support and Admin role management API."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.session import get_session
from app.modules.identity import service
from app.modules.identity.auth import get_current_user
from app.modules.identity.enums import AccountStatus
from app.modules.identity.models import User
from app.modules.identity.request_limits import VerifiedIdentity, require_authenticated_rate_limit
from app.modules.identity.schemas import (
    AccountDecision,
    AccountSupportErrorResponse,
    ActionResult,
    AdminRoleInput,
    ReasonInput,
    UsersRead,
    UsersReadPage,
)

Actor = Annotated[User, Depends(get_current_user)]
Session = Annotated[AsyncSession, Depends(get_session)]
SEARCH_PARAMETERS = {"q", "status", "page", "page_size"}


def prevent_caching(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(
    prefix="/admin",
    tags=["Account support"],
    dependencies=[Depends(require_authenticated_rate_limit), Depends(prevent_caching)],
    responses={
        401: {"model": AccountSupportErrorResponse, "description": "Authentication required."},
        403: {"model": AccountSupportErrorResponse, "description": "Permission denied."},
        404: {"model": AccountSupportErrorResponse, "description": "Account unavailable."},
        409: {"model": AccountSupportErrorResponse, "description": "Invalid account state."},
        422: {"model": AccountSupportErrorResponse, "description": "Invalid input."},
        429: {
            "model": AccountSupportErrorResponse,
            "description": "Rate limit exceeded.",
            "headers": {"Retry-After": {"schema": {"type": "integer"}}},
        },
        500: {"model": AccountSupportErrorResponse, "description": "Unexpected error."},
        503: {"model": AccountSupportErrorResponse, "description": "Service unavailable."},
    },
)


@router.get("/users", response_model=UsersReadPage)
async def list_support_users(
    request: Request,
    actor: Actor,
    session: Session,
    q: Annotated[str | None, Query(max_length=200, pattern=r"^[^\x00]*$")] = None,
    status: AccountStatus | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> UsersReadPage:
    """Search operational account information within delegated support permissions."""
    if set(request.query_params) - SEARCH_PARAMETERS:
        raise RequestValidationError([{"loc": ("query",)}])
    return await service.search_accounts(
        session,
        request,
        actor.id,
        query=q.strip() if q else None,
        status=status,
        page=page,
        page_size=page_size,
    )


@router.post("/users/{user_id}/status", response_model=UsersRead, status_code=201)
async def update_support_user_status(
    user_id: UUID,
    decision: AccountDecision,
    request: Request,
    actor: Actor,
    identity: VerifiedIdentity,
    session: Session,
) -> UsersRead:
    """Suspend, disable or reactivate an account with an audited decision."""
    return await service.decide_account_status(
        session, request, actor.id, identity, user_id, decision
    )


@router.post("/role-assignments", response_model=ActionResult, status_code=201)
async def appoint_admin(
    decision: AdminRoleInput,
    request: Request,
    actor: Actor,
    identity: VerifiedIdentity,
    session: Session,
) -> ActionResult:
    """Allow an MFA-authenticated Super Admin to appoint an Admin."""
    return await service.change_admin_assignment(
        session, request, actor.id, identity, decision.user_id, decision.reason, revoke=False
    )


@router.post("/users/{user_id}/admin-role/revoke", response_model=ActionResult, status_code=201)
async def revoke_admin(
    user_id: UUID,
    decision: ReasonInput,
    request: Request,
    actor: Actor,
    identity: VerifiedIdentity,
    session: Session,
) -> ActionResult:
    """Allow an MFA-authenticated Super Admin to revoke an Admin assignment."""
    return await service.change_admin_assignment(
        session, request, actor.id, identity, user_id, decision.reason, revoke=True
    )
