"""API routes for organizations and memberships."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.session import get_session
from app.modules.identity.auth import get_current_user
from app.modules.identity.models import User
from app.modules.organizations import service
from app.modules.organizations.schemas import (
    MembershipDecision,
    MembershipInput,
    MembershipRead,
    MembershipReadPage,
    OrganizationDecision,
    OrganizationInput,
    OrganizationPublicPage,
    OrganizationRead,
    OrganizationReadPage,
)

router = APIRouter(tags=["Organizations"])
Session = Annotated[AsyncSession, Depends(get_session)]
Actor = Annotated[User, Depends(get_current_user)]


@router.get("/organizations", response_model=OrganizationPublicPage)
async def get_organizations(
    request: Request,
    db: Session,
    actor: Actor,
    type: Annotated[
        str | None, Query(pattern="^(clinic|hospital|pharmacy|laboratory|other)$")
    ] = None,
    q: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
):
    return await service.list_public_organizations(db, actor, request, type, q, page, page_size)


@router.post("/organizations", response_model=OrganizationRead, status_code=status.HTTP_201_CREATED)
async def post_organization(
    payload: OrganizationInput, request: Request, db: Session, actor: Actor
):
    return await service.submit_organization(db, actor, request, payload)


@router.post("/me/memberships", response_model=MembershipRead, status_code=status.HTTP_201_CREATED)
async def post_membership(payload: MembershipInput, request: Request, db: Session, actor: Actor):
    return await service.request_membership(db, actor, request, payload)


@router.get("/admin/organizations", response_model=OrganizationReadPage)
async def get_admin_organizations(
    request: Request,
    db: Session,
    actor: Actor,
    status_filter: Annotated[
        str | None, Query(alias="status", pattern="^(pending|active|suspended|rejected|inactive)$")
    ] = None,
    type: Annotated[
        str | None, Query(pattern="^(clinic|hospital|pharmacy|laboratory|other)$")
    ] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
):
    return await service.admin_list_organizations(
        db, actor, request, status_filter, type, page, page_size
    )


@router.get("/admin/memberships", response_model=MembershipReadPage)
async def get_admin_memberships(
    request: Request,
    db: Session,
    actor: Actor,
    organization_id: UUID | None = None,
    status_filter: Annotated[
        str | None,
        Query(
            alias="status",
            pattern="^(not_submitted|pending|approved|rejected|more_info_requested|suspended)$",
        ),
    ] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
):
    return await service.admin_list_memberships(
        db, actor, request, organization_id, status_filter, page, page_size
    )


@router.post(
    "/admin/organizations/{organization_id}/decision",
    response_model=OrganizationRead,
    status_code=status.HTTP_201_CREATED,
)
async def post_organization_decision(
    organization_id: UUID,
    payload: OrganizationDecision,
    request: Request,
    db: Session,
    actor: Actor,
):
    return await service.decide_organization(db, actor, request, organization_id, payload)


@router.post(
    "/admin/memberships/{membership_id}/decision",
    response_model=MembershipRead,
    status_code=status.HTTP_201_CREATED,
)
async def post_membership_decision(
    membership_id: UUID, payload: MembershipDecision, request: Request, db: Session, actor: Actor
):
    return await service.decide_membership(db, actor, request, membership_id, payload)
