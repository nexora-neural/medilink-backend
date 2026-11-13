"""Database queries for organization and membership workflows."""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.organizations.models import Organization, OrganizationMembership


async def list_organizations(
    session: AsyncSession,
    *,
    public: bool,
    status: str | None,
    organization_type: str | None,
    q: str | None,
    page: int,
    page_size: int,
):
    statement = select(Organization)
    if public:
        statement = statement.where(Organization.status == "active")
    elif status:
        statement = statement.where(Organization.status == status)
    if organization_type:
        statement = statement.where(Organization.type == organization_type)
    if q:
        statement = statement.where(Organization.name.ilike(f"%{q}%"))
    total = await session.scalar(select(func.count()).select_from(statement.subquery())) or 0
    rows = (
        await session.scalars(
            statement.order_by(Organization.name, Organization.id)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).all()
    return rows, total


async def list_memberships(
    session: AsyncSession, *, organization_id, status: str | None, page: int, page_size: int
):
    statement = select(OrganizationMembership)
    if organization_id is not None:
        statement = statement.where(OrganizationMembership.organization_id == organization_id)
    if status:
        statement = statement.where(OrganizationMembership.status == status)
    total = await session.scalar(select(func.count()).select_from(statement.subquery())) or 0
    rows = (
        await session.scalars(
            statement.order_by(OrganizationMembership.created_at.desc(), OrganizationMembership.id)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).all()
    return rows, total


async def get_organization(session: AsyncSession, organization_id, *, lock: bool = False):
    query = select(Organization).where(Organization.id == organization_id)
    if lock:
        query = query.with_for_update()
    return await session.scalar(query)


async def get_membership(session: AsyncSession, membership_id, *, lock: bool = False):
    query = select(OrganizationMembership).where(OrganizationMembership.id == membership_id)
    if lock:
        query = query.with_for_update()
    return await session.scalar(query)


async def get_existing_membership(
    session: AsyncSession, organization_id, user_id, role: str, *, lock: bool = False
):
    query = select(OrganizationMembership).where(
        OrganizationMembership.organization_id == organization_id,
        OrganizationMembership.user_id == user_id,
        OrganizationMembership.role == role,
    )
    if lock:
        query = query.with_for_update()
    return await session.scalar(query)
