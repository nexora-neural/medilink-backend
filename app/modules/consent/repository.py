"""Database access methods for patient consent and access grants."""


from collections import defaultdict
from datetime import datetime
from uuid import UUID

from sqlalchemy import BigInteger, String, bindparam, case, cast, func, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.consent.models import AccessGrant, GrantCategory
from app.modules.consent.schemas import GrantCategoryName, GrantListQuery
from app.modules.profiles.models import PatientProfile


async def has_patient_profile(db: AsyncSession, patient_id: UUID) -> bool:
    return await db.get(PatientProfile, patient_id) is not None


async def insert_grant(
    db: AsyncSession, grant: AccessGrant, categories: list[GrantCategoryName]
) -> None:
    db.add(grant)
    await db.flush()
    db.add_all([GrantCategory(grant_id=grant.id, category=category) for category in categories])
    await db.flush()


async def lock_owned_grant(
    db: AsyncSession, patient_id: UUID, grant_id: UUID
) -> AccessGrant | None:
    return await db.scalar(
        select(AccessGrant)
        .where(AccessGrant.id == grant_id, AccessGrant.patient_user_id == patient_id)
        .with_for_update()
    )


async def list_owned_grants(
    db: AsyncSession, patient_id: UUID, query: GrantListQuery, now: datetime
) -> tuple[list[AccessGrant], int]:
    effective_status = case(
        (
            (AccessGrant.status == "active") & (AccessGrant.expires_at <= now),
            "expired",
        ),
        else_=cast(AccessGrant.status, String),
    )
    eligible = select(AccessGrant.id, AccessGrant.created_at).where(
        AccessGrant.patient_user_id == patient_id
    )
    if query.status is not None:
        eligible = eligible.where(effective_status == query.status)
    eligible = eligible.cte("eligible_grants")
    totals = select(func.count().label("total")).select_from(eligible).subquery()
    offset = min((query.page - 1) * query.page_size, 2**63 - 1)
    page = (
        select(eligible.c.id)
        .order_by(eligible.c.created_at.desc(), eligible.c.id.desc())
        .limit(query.page_size)
        .offset(bindparam("grant_offset", offset, type_=BigInteger))
        .subquery()
    )
    rows = (
        await db.execute(
            select(AccessGrant, totals.c.total)
            .select_from(totals.outerjoin(page, true()))
            .outerjoin(AccessGrant, AccessGrant.id == page.c.id)
            .order_by(AccessGrant.created_at.desc(), AccessGrant.id.desc())
        )
    ).all()
    return [grant for grant, _ in rows if grant is not None], rows[0].total


async def categories_by_grant(
    db: AsyncSession, grant_ids: list[UUID]
) -> dict[UUID, list[str]]:
    categories: dict[UUID, list[str]] = defaultdict(list)
    if grant_ids:
        result = await db.execute(
            select(GrantCategory.grant_id, GrantCategory.category)
            .where(GrantCategory.grant_id.in_(grant_ids))
            .order_by(cast(GrantCategory.category, String))
        )
        for grant_id, category in result:
            categories[grant_id].append(category)
    return categories
