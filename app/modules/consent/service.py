"""Business rules and orchestration for patient consent and access grants."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.audit.service import append_event
from app.modules.consent import repository
from app.modules.consent.models import AccessGrant
from app.modules.consent.schemas import (
    AccessGrantsRead,
    GrantDetail,
    GrantDetailPage,
    GrantInput,
    GrantListQuery,
)
from app.modules.identity.authorization import (
    deny,
    require_role,
    require_verified_membership,
)
from app.modules.identity.models import User
from app.modules.profiles.doctor_qr.repository import find_active_doctor_by_reference


async def require_patient(db: AsyncSession, actor: User) -> None:
    await require_role(db, actor, "patient")
    if not await repository.has_patient_profile(db, actor.id):
        deny()


def effective_status(grant: AccessGrant, now: datetime) -> str:
    if grant.status == "active" and grant.expires_at <= now:
        return "expired"
    return grant.status


def grant_detail(grant: AccessGrant, categories: list[str], now: datetime) -> GrantDetail:
    header = AccessGrantsRead.model_validate(grant)
    if effective_status(grant, now) == "expired":
        header.status = "expired"
    return GrantDetail(grant=header, categories=categories)


async def create_grant(
    db: AsyncSession, actor: User, body: GrantInput, request_id: str
) -> GrantDetail:
    await require_patient(db, actor)
    resolved = await find_active_doctor_by_reference(db, body.doctor_reference)
    if resolved is None:
        raise HTTPException(404, "Doctor reference was not found.")

    doctor, profile = resolved
    if doctor.id == actor.id or profile.verification_status != "approved":
        deny()
    if body.organization_id is None:
        await require_role(db, doctor, "doctor")
    else:
        await require_verified_membership(db, doctor, "doctor", body.organization_id)

    now = datetime.now(UTC)
    grant = AccessGrant(
        patient_user_id=actor.id,
        professional_user_id=doctor.id,
        organization_id=body.organization_id,
        allow_consultation=body.allow_consultation,
        status="active",
        starts_at=now,
        expires_at=now + timedelta(hours=body.duration_hours),
    )
    await repository.insert_grant(db, grant, body.categories)
    detail = grant_detail(grant, sorted(body.categories), now)
    await append_event(
        db,
        actor_user_id=actor.id,
        target_type="access_grant",
        target_id=grant.id,
        action="access_grant.created",
        outcome="success",
        request_id=request_id,
        metadata={"status": "active"},
    )
    await db.commit()
    return detail


async def list_grants(
    db: AsyncSession, actor: User, query: GrantListQuery
) -> GrantDetailPage:
    await require_patient(db, actor)
    now = datetime.now(UTC)
    grants, total = await repository.list_owned_grants(db, actor.id, query, now)
    categories = await repository.categories_by_grant(db, [grant.id for grant in grants])
    return GrantDetailPage(
        items=[grant_detail(grant, categories.get(grant.id, []), now) for grant in grants],
        page=query.page,
        page_size=query.page_size,
        total=total,
    )


async def revoke_grant(
    db: AsyncSession, actor: User, grant_id: UUID, reason: str, request_id: str
) -> GrantDetail:
    await require_patient(db, actor)
    grant = await repository.lock_owned_grant(db, actor.id, grant_id)
    if grant is None:
        raise HTTPException(404, "Access grant was not found.")

    now = datetime.now(UTC)
    if effective_status(grant, now) != "active":
        raise HTTPException(409, "Only an active access grant can be revoked.")

    grant.status = "revoked"
    grant.revoked_at = now
    grant.revocation_reason = reason
    categories = await repository.categories_by_grant(db, [grant.id])
    detail = grant_detail(grant, categories.get(grant.id, []), now)
    await append_event(
        db,
        actor_user_id=actor.id,
        target_type="access_grant",
        target_id=grant.id,
        action="access_grant.revoked",
        outcome="success",
        request_id=request_id,
        metadata={"previous_status": "active", "status": "revoked"},
    )
    await db.commit()
    return detail
