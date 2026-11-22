"""Business rules for consultations and the doctor patient queue."""

from datetime import UTC, datetime
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.audit.service import append_event
from app.modules.consent import repository as consent_repository
from app.modules.consent.service import require_patient
from app.modules.consultations import repository
from app.modules.consultations.models import Consultation
from app.modules.consultations.schemas import (
    ConsultationEnd,
    ConsultationInput,
    ConsultationListQuery,
    ConsultationsRead,
    ConsultationsReadPage,
    PatientIdentity,
    QueueEntry,
    QueueEntryPage,
)
from app.modules.identity.authorization import (
    deny,
    require_verified_membership,
    require_verified_professional,
)
from app.modules.identity.models import User


async def list_patient_queue(
    db: AsyncSession,
    actor: User,
    query: ConsultationListQuery,
) -> QueueEntryPage:
    await require_verified_professional(db, actor, "doctor")

    now = datetime.now(UTC)
    rows, total = await repository.list_doctor_queue(
        db,
        actor.id,
        query,
        now,
    )

    categories = await consent_repository.categories_by_grant(
        db,
        [grant.id for grant, _, _ in rows],
    )

    return QueueEntryPage(
        items=[
            QueueEntry(
                patient=PatientIdentity(
                    patient_user_id=grant.patient_user_id,
                    display_name=display_name,
                    date_of_birth=date_of_birth,
                ),
                grant_id=grant.id,
                categories=categories.get(grant.id, []),
                expires_at=grant.expires_at,
                allow_consultation=grant.allow_consultation,
            )
            for grant, display_name, date_of_birth in rows
        ],
        page=query.page,
        page_size=query.page_size,
        total=total,
    )


async def start_consultation(
    db: AsyncSession,
    actor: User,
    body: ConsultationInput,
    request_id: str,
) -> ConsultationsRead:
    await require_verified_professional(db, actor, "doctor")

    grant = await repository.lock_doctor_grant(
        db,
        actor.id,
        body.grant_id,
    )

    if grant is None:
        raise HTTPException(
            status_code=404,
            detail="Access grant was not found.",
        )

    if grant.organization_id is not None:
        await require_verified_membership(
            db,
            actor,
            "doctor",
            grant.organization_id,
        )

    patient = await db.get(User, grant.patient_user_id)

    if patient is None or patient.status != "active":
        deny()

    categories = await consent_repository.categories_by_grant(
        db,
        [grant.id],
    )

    # Check time after acquiring the lock and completing the other checks.
    now = datetime.now(UTC)

    if (
        grant.status != "active"
        or grant.revoked_at is not None
        or grant.starts_at > now
        or grant.expires_at <= now
        or not grant.allow_consultation
        or not categories.get(grant.id)
    ):
        deny()

    consultation = Consultation(
        patient_user_id=grant.patient_user_id,
        doctor_user_id=actor.id,
        organization_id=grant.organization_id,
        grant_id=grant.id,
        started_at=now,
    )

    await repository.insert_consultation(db, consultation)

    response = ConsultationsRead.model_validate(consultation)

    await append_event(
        db,
        actor_user_id=actor.id,
        target_type="consultation",
        target_id=consultation.id,
        action="consultation.started",
        outcome="success",
        request_id=request_id,
        metadata={},
    )

    await db.commit()
    return response


async def end_consultation(
    db: AsyncSession,
    actor: User,
    consultation_id: UUID,
    body: ConsultationEnd,
    request_id: str,
) -> ConsultationsRead:
    await require_verified_professional(db, actor, "doctor")

    existing = await repository.find_owned_consultation(
        db,
        actor.id,
        consultation_id,
    )

    if existing is None:
        raise HTTPException(
            status_code=404,
            detail="Consultation was not found.",
        )

    # Lock the grant first, consistently with consultation creation.
    grant = await repository.lock_doctor_grant(
        db,
        actor.id,
        existing.grant_id,
    )

    if grant is None:
        deny()

    if grant.organization_id is not None:
        await require_verified_membership(
            db,
            actor,
            "doctor",
            grant.organization_id,
        )

    patient = await db.get(User, grant.patient_user_id)

    if patient is None or patient.status != "active":
        deny()

    consultation = await repository.find_owned_consultation(
        db,
        actor.id,
        consultation_id,
        lock=True,
    )

    if (
        consultation is None
        or consultation.grant_id != grant.id
        or consultation.patient_user_id != grant.patient_user_id
        or consultation.organization_id != grant.organization_id
    ):
        deny()

    categories = await consent_repository.categories_by_grant(
        db,
        [grant.id],
    )

    now = datetime.now(UTC)

    if (
        grant.status != "active"
        or grant.revoked_at is not None
        or grant.starts_at > now
        or grant.expires_at <= now
        or not grant.allow_consultation
        or not categories.get(grant.id)
        or consultation.started_at > now
    ):
        deny()

    if consultation.ended_at is not None:
        return ConsultationsRead.model_validate(consultation)

    consultation.ended_at = now
    consultation.summary = body.summary.strip() or None

    await db.flush()
    response = ConsultationsRead.model_validate(consultation)

    await append_event(
        db,
        actor_user_id=actor.id,
        target_type="consultation",
        target_id=consultation.id,
        action="consultation.ended",
        outcome="success",
        request_id=request_id,
        metadata={},
    )

    await db.commit()
    return response


async def list_consultations(
    db: AsyncSession,
    actor: User,
    patient_id: UUID,
    query: ConsultationListQuery,
) -> ConsultationsReadPage:
    doctor_id: UUID | None

    if actor.id == patient_id:
        await require_patient(db, actor)
        doctor_id = None
    else:
        await require_verified_professional(db, actor, "doctor")

        patient = await db.get(User, patient_id)

        if patient is None or patient.status != "active":
            deny()

        doctor_id = actor.id

    rows, total = await repository.list_patient_consultations(
        db,
        patient_id,
        query,
        doctor_id=doctor_id,
    )

    return ConsultationsReadPage(
        items=[ConsultationsRead.model_validate(consultation) for consultation in rows],
        page=query.page,
        page_size=query.page_size,
        total=total,
    )
