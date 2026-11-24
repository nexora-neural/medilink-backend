"""Current patient/doctor permissions, append-only creation and transactional audit."""

from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.audit.service import append_event
from app.modules.clinical_records import repository
from app.modules.clinical_records.schemas import (
    ClinicalInput,
    ClinicalListQuery,
    ClinicalRecordRead,
    ClinicalRecordReadPage,
)
from app.modules.identity.models import User
from app.modules.profiles.models import PatientProfile


def missing():
    raise HTTPException(404, "Clinical resource was not found.")


async def access(db, actor: User, patient_id: UUID, body: ClinicalInput | None = None):
    # Share locks prevent status/role/verification/grant/context revocation from
    # racing a committed clinical write. Recheck expiry after acquiring locks.
    status = await db.scalar(
        select(User.status)
        .where(User.id == actor.id)
        .with_for_update(
            read=True,
        )
    )
    if status != "active":
        raise HTTPException(403, "An active account is required.")
    roles = await repository.current_roles(db, actor.id)
    if actor.id == patient_id and "patient" in roles:
        if await db.get(PatientProfile, patient_id) is None:
            missing()
        if body is not None:
            if body.type not in {"condition", "allergy"}:
                raise HTTPException(403, "Patients may record conditions and allergies only.")
            if body.consultation_id is not None:
                raise HTTPException(422, "Omit consultation_id for a patient entry.")
        return "patient", None
    if "doctor" not in roles:
        missing()
    if await repository.verified_doctor(db, actor.id) is None:
        raise HTTPException(403, "Doctor verification is required.")
    if body is not None and body.consultation_id is None:
        await access(db, actor, patient_id)
        raise HTTPException(422, "consultation_id is required for doctor entries.")
    candidates = await repository.grants(
        db,
        actor.id,
        patient_id,
        body.consultation_id if body is not None else None,
    )
    for grant in candidates:
        if (
            grant.organization_id is not None
            and await repository.approved_membership(
                db,
                actor.id,
                grant.organization_id,
            )
            is None
        ):
            continue
        patient_status = await db.scalar(
            select(User.status)
            .join(
                PatientProfile,
                PatientProfile.user_id == User.id,
            )
            .where(User.id == patient_id)
            .with_for_update(read=True, of=User)
        )
        now = await db.scalar(select(func.clock_timestamp()))
        if not (grant.starts_at <= now < grant.expires_at):
            continue
        if body is not None and not await repository.consultation_started(
            db,
            body.consultation_id,
            now,
        ):
            continue
        if patient_status != "active":
            raise HTTPException(403, "Patient account is inactive.")
        return "doctor", grant.organization_id
    # Conceal another patient's record and expired/revoked/foreign contexts.
    missing()


async def list_patient_records(db, actor, patient_id, query: ClinicalListQuery, request_id):
    try:
        await access(db, actor, patient_id)
        page = ClinicalRecordReadPage.model_validate(
            await repository.list_records(db, patient_id, query),
        )
        await append_event(
            db,
            actor_user_id=actor.id,
            action="clinical_record.listed",
            target_type="patient_record",
            target_id=patient_id,
            outcome="success",
            request_id=request_id,
            metadata={"result_count": len(page.items)},
        )
        await db.commit()
        return page
    except Exception:
        await db.rollback()
        raise


async def get_record(db, actor, record_id, request_id):
    try:
        record = await repository.find_record(db, record_id)
        if record is None:
            missing()
        await access(db, actor, record["patient_user_id"])
        response = ClinicalRecordRead.model_validate(record)
        await append_event(
            db,
            actor_user_id=actor.id,
            action="clinical_record.viewed",
            target_type="clinical_record",
            target_id=record_id,
            outcome="success",
            request_id=request_id,
            metadata={"type": response.type},
        )
        await db.commit()
        return response
    except Exception:
        await db.rollback()
        raise


async def create_record(
    db: AsyncSession,
    actor: User,
    patient_id: UUID,
    body: ClinicalInput,
    request_id: str,
):
    try:
        source, organization_id = await access(db, actor, patient_id, body)
        response = ClinicalRecordRead.model_validate(
            await repository.insert_record(
                db,
                patient_id,
                actor.id,
                body,
                source,
                organization_id,
            )
        )
        await append_event(
            db,
            actor_user_id=actor.id,
            action="clinical_record.created",
            target_type="clinical_record",
            target_id=response.id,
            outcome="success",
            request_id=request_id,
            metadata={"type": body.type, "source": source},
        )
        await db.commit()
        return response
    except Exception:
        await db.rollback()
        raise
