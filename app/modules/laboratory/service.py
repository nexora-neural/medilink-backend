"""Business rules for patient lab upload requests."""

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.audit.service import append_event
from app.modules.identity.authorization import require_role, require_verified_membership
from app.modules.identity.models import User
from app.modules.laboratory import repository
from app.modules.laboratory.models import LabUploadRequest, UploadRequestStatus
from app.modules.laboratory.schemas import (
    LabUploadRequestCreated,
    LabUploadRequestPage,
    LabUploadRequestRead,
    LabUploadRequestResolved,
    PatientIdentity,
)


class LabUploadRequestError(Exception):
    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def require_patient(db: AsyncSession, actor: User) -> None:
    await require_role(db, actor, "patient")
    if not await repository.patient_profile_exists(db, actor.id):
        raise LabUploadRequestError(404, "NOT_FOUND", "Patient profile not found.")


async def create_request(
    db: AsyncSession,
    actor: User,
    laboratory_id: UUID,
    audit_request_id: str,
) -> LabUploadRequestCreated:
    await require_patient(db, actor)
    laboratory = await repository.get_laboratory(db, laboratory_id)
    if laboratory is None:
        raise LabUploadRequestError(404, "NOT_FOUND", "Laboratory not found.")
    if laboratory.status != "active":
        raise LabUploadRequestError(403, "FORBIDDEN", "Laboratory is inactive.")

    now = datetime.now(UTC)
    token = secrets.token_urlsafe(32)
    upload_request = LabUploadRequest(
        patient_user_id=actor.id,
        laboratory_id=laboratory_id,
        request_token_hash=hash_token(token),
        status=UploadRequestStatus.ACTIVE,
        created_at=now,
        expires_at=now + timedelta(days=7),
    )
    db.add(upload_request)
    await db.flush()

    result = LabUploadRequestCreated(
        request=LabUploadRequestRead.model_validate(upload_request),
        request_token=token,
    )
    await append_event(
        db,
        actor_user_id=actor.id,
        action="lab_upload_request.created",
        target_type="lab_upload_request",
        target_id=upload_request.id,
        outcome="success",
        request_id=audit_request_id,
        metadata={"organization_id": str(laboratory_id), "status": "active"},
    )
    await db.commit()
    return result


async def list_requests(
    db: AsyncSession,
    actor: User,
    *,
    page: int,
    page_size: int,
) -> LabUploadRequestPage:
    await require_patient(db, actor)
    rows, total = await repository.list_patient_requests(
        db, actor.id, page=page, page_size=page_size
    )
    now = datetime.now(UTC)
    items = []
    for row in rows:
        item = LabUploadRequestRead.model_validate(row)
        if item.status == UploadRequestStatus.ACTIVE and item.expires_at <= now:
            item.status = UploadRequestStatus.EXPIRED
        items.append(item)

    return LabUploadRequestPage(items=items, page=page, page_size=page_size, total=total)


async def revoke_request(
    db: AsyncSession,
    actor: User,
    request_id: UUID,
    reason: str,
    audit_request_id: str,
) -> LabUploadRequestRead:
    await require_patient(db, actor)
    upload_request = await repository.lock_patient_request(db, request_id, actor.id)
    if upload_request is None:
        raise LabUploadRequestError(404, "NOT_FOUND", "Upload request not found.")
    if (
        upload_request.status != UploadRequestStatus.ACTIVE
        or upload_request.used_at is not None
        or upload_request.expires_at <= datetime.now(UTC)
    ):
        raise LabUploadRequestError(
            409, "INVALID_STATE", "Only an active, unused request can be revoked."
        )

    upload_request.status = UploadRequestStatus.REVOKED
    upload_request.revocation_reason = reason
    result = LabUploadRequestRead.model_validate(upload_request)

    await append_event(
        db,
        actor_user_id=actor.id,
        action="lab_upload_request.revoked",
        target_type="lab_upload_request",
        target_id=upload_request.id,
        outcome="success",
        request_id=audit_request_id,
        metadata={
            "organization_id": str(upload_request.laboratory_id),
            "previous_status": "active",
            "status": "revoked",
        },
    )
    await db.commit()
    return result


async def resolve_request(
    db: AsyncSession,
    actor: User,
    laboratory_id: UUID,
    token: str,
    audit_request_id: str,
) -> LabUploadRequestResolved:
    await require_verified_membership(db, actor, "lab_staff", laboratory_id)
    row = await repository.find_resolvable_request(
        db,
        laboratory_id=laboratory_id,
        token_hash=hash_token(token),
        now=datetime.now(UTC),
    )
    if row is None:
        raise LabUploadRequestError(404, "NOT_FOUND", "Upload request not found.")

    result = LabUploadRequestResolved(
        request_id=row["request_id"],
        laboratory_id=row["laboratory_id"],
        expires_at=row["expires_at"],
        patient=PatientIdentity(
            patient_user_id=row["patient_user_id"],
            display_name=row["display_name"],
            date_of_birth=row["date_of_birth"],
        ),
    )
    await append_event(
        db,
        actor_user_id=actor.id,
        action="lab_upload_request.resolved",
        target_type="lab_upload_request",
        target_id=result.request_id,
        outcome="success",
        request_id=audit_request_id,
        metadata={"organization_id": str(laboratory_id)},
    )
    await db.commit()
    return result