"""Current patient and doctor authorization for prescription operations."""

from datetime import datetime
from uuid import UUID

from fastapi import Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.clinical_records.repository import (
    approved_membership,
    current_roles,
    verified_doctor,
)
from app.modules.consent.models import AccessGrant, GrantCategory
from app.modules.consultations.models import Consultation
from app.modules.identity.auth import AuthenticationError
from app.modules.identity.models import User
from app.modules.profiles.models import PatientProfile


def deny_access(request: Request) -> None:
    raise AuthenticationError(
        status_code=403,
        detail={
            "code": "ACCESS_DENIED",
            "message": "Permission denied.",
            "field_errors": {},
            "request_id": getattr(request.state, "request_id", ""),
        },
    )


async def prescription_grants(
    session: AsyncSession,
    *,
    actor_id: UUID,
    patient_id: UUID,
    consultation_id: UUID | None,
    clinical_write: bool,
) -> list[AccessGrant]:
    statement = (
        select(AccessGrant)
        .join(
            GrantCategory,
            GrantCategory.grant_id == AccessGrant.id,
        )
        .where(
            AccessGrant.patient_user_id == patient_id,
            AccessGrant.professional_user_id == actor_id,
            GrantCategory.category == "prescriptions",
            AccessGrant.status == "active",
            AccessGrant.revoked_at.is_(None),
        )
    )

    locks = (AccessGrant, GrantCategory)

    if clinical_write:
        statement = statement.join(
            Consultation,
            Consultation.grant_id == AccessGrant.id,
        ).where(
            Consultation.id == consultation_id,
            Consultation.patient_user_id == patient_id,
            Consultation.doctor_user_id == actor_id,
            Consultation.organization_id.is_not_distinct_from(
                AccessGrant.organization_id
            ),
            Consultation.ended_at.is_(None),
            AccessGrant.allow_consultation.is_(True),
        )
        locks += (Consultation,)

    result = await session.scalars(
        statement.order_by(AccessGrant.id).with_for_update(
            read=True,
            of=locks,
        )
    )
    return list(result.all())


async def require_prescription_access(
    session: AsyncSession,
    *,
    user: User,
    request: Request,
    patient_id: UUID,
    consultation_id: UUID | None = None,
    clinical_write: bool = False,
) -> str:
    """Return the authorized source; leave transaction control to the caller."""

    actor_status = await session.scalar(
        select(User.status)
        .where(User.id == user.id)
        .with_for_update(read=True)
    )
    if actor_status != "active":
        deny_access(request)

    roles = await current_roles(session, user.id)

    if user.id == patient_id and "patient" in roles:
        patient = await session.scalar(
            select(PatientProfile.user_id)
            .where(PatientProfile.user_id == patient_id)
            .with_for_update(read=True)
        )
        if patient is None:
            deny_access(request)

        # Patient-entered records cannot claim a doctor's consultation.
        if clinical_write and consultation_id is not None:
            deny_access(request)

        return "patient"

    if "doctor" not in roles:
        deny_access(request)

    if not await verified_doctor(session, user.id):
        deny_access(request)

    if clinical_write and consultation_id is None:
        deny_access(request)

    grants = await prescription_grants(
        session,
        actor_id=user.id,
        patient_id=patient_id,
        consultation_id=consultation_id,
        clinical_write=clinical_write,
    )

    for grant in grants:
        if grant.organization_id is not None:
            membership = await approved_membership(
                session,
                user.id,
                grant.organization_id,
            )
            if membership is None:
                continue

        patient_status = await session.scalar(
            select(User.status)
            .join(
                PatientProfile,
                PatientProfile.user_id == User.id,
            )
            .where(User.id == patient_id)
            .with_for_update(read=True, of=(User, PatientProfile))
        )
        if patient_status != "active":
            continue

        # Evaluate expiry after acquiring the authorization locks.
        now: datetime = await session.scalar(
            select(func.clock_timestamp())
        )
        if not (grant.starts_at <= now < grant.expires_at):
            continue

        if clinical_write:
            started = await session.scalar(
                select(Consultation.id).where(
                    Consultation.id == consultation_id,
                    Consultation.started_at <= now,
                )
            )
            if started is None:
                continue

        return "doctor"

    deny_access(request)