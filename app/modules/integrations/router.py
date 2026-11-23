"""API router for Super Admin integration client management."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.exceptions import RequestValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.session import get_session
from app.modules.identity.auth import get_current_user
from app.modules.identity.models import User
from app.modules.identity.request_limits import require_authenticated_rate_limit
from app.modules.integrations import service
from app.modules.integrations.schemas import (
    CredentialCreated,
    EligibleLaboratoriesReadPage,
    IntegrationClientInput,
    IntegrationClientRead,
    IntegrationClientsReadPage,
    IntegrationSubmissionsReadPage,
    ReasonInput,
    RotateCredential,
)
from app.modules.profiles.route import ProfileRoute

router = APIRouter(
    tags=["Integration management"],
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


@router.get(
    "/admin/integration-clients/eligible-laboratories",
    response_model=EligibleLaboratoriesReadPage,
)
async def get_eligible_laboratories(
    request: Request,
    db: DB,
    actor: Actor,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> EligibleLaboratoriesReadPage:
    """Return active laboratories with an eligible approved Lab Staff member."""
    _reject_unknown_query_params(request, {"page", "page_size"})
    return await service.list_eligible_laboratories(
        db, actor, request, page=page, page_size=page_size
    )


@router.post(
    "/admin/integration-clients",
    response_model=CredentialCreated,
    status_code=status.HTTP_201_CREATED,
)
async def post_integration_client(
    payload: IntegrationClientInput, request: Request, db: DB, actor: Actor
) -> CredentialCreated:
    _reject_unknown_query_params(request, set())
    return await service.create_client(db, actor, request, payload)


@router.get("/admin/integration-clients", response_model=IntegrationClientsReadPage)
async def get_integration_clients(
    request: Request,
    db: DB,
    actor: Actor,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> IntegrationClientsReadPage:
    _reject_unknown_query_params(request, {"page", "page_size"})
    return await service.list_clients(
        db, actor, request, page=page, page_size=page_size
    )


@router.post(
    "/admin/integration-clients/{client_id}/rotate",
    response_model=CredentialCreated,
    status_code=status.HTTP_201_CREATED,
)
async def post_rotate_integration_client(
    client_id: UUID, payload: RotateCredential, request: Request, db: DB, actor: Actor
) -> CredentialCreated:
    _reject_unknown_query_params(request, set())
    return await service.rotate_client_credential(db, actor, request, client_id, payload)


@router.post(
    "/admin/integration-clients/{client_id}/revoke",
    response_model=IntegrationClientRead,
    status_code=status.HTTP_201_CREATED,
)
async def post_revoke_integration_client(
    client_id: UUID, payload: ReasonInput, request: Request, db: DB, actor: Actor
) -> IntegrationClientRead:
    _reject_unknown_query_params(request, set())
    return await service.revoke_client(db, actor, request, client_id, payload)


@router.post(
    "/admin/integration-clients/{client_id}/suspend",
    response_model=IntegrationClientRead,
    status_code=status.HTTP_201_CREATED,
)
async def post_suspend_integration_client(
    client_id: UUID, payload: ReasonInput, request: Request, db: DB, actor: Actor
) -> IntegrationClientRead:
    _reject_unknown_query_params(request, set())
    return await service.suspend_client(db, actor, request, client_id, payload)


@router.get("/admin/integration-submissions", response_model=IntegrationSubmissionsReadPage)
async def get_integration_submissions(
    request: Request,
    db: DB,
    actor: Actor,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    status_filter: str | None = Query(default=None, alias="status", max_length=200),
) -> IntegrationSubmissionsReadPage:
    _reject_unknown_query_params(request, {"page", "page_size", "status"})
    return await service.list_submissions(
        db,
        actor,
        request,
        page=page,
        page_size=page_size,
        status_filter=status_filter,
    )
