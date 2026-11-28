"""Patient-owned prescription OCR API orchestration."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from fastapi import Request
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.idempotency import (
    IdempotencyConflict,
    IdempotencyScope,
    lock_and_replay,
    request_digest,
    save_success,
)
from app.modules.audit.service import append_event
from app.modules.documents.models import Document
from app.modules.identity.auth import AuthenticationError
from app.modules.identity.models import User
from app.modules.prescriptions import repository
from app.modules.prescriptions import service as prescription_service
from app.modules.prescriptions.access import require_prescription_access
from app.modules.prescriptions.ocr_template_models import PrescriptionOcrJobModel
from app.modules.prescriptions.schemas import (
    PrescriptionDetail,
    PrescriptionInput,
    PrescriptionOcrDraftCreated,
    PrescriptionOcrDraftInput,
    PrescriptionOcrJobInput,
    PrescriptionOcrJobRead,
    PrescriptionOcrJobReadPage,
    PrescriptionOcrRetryInput,
)

ALLOWED_MEDIA_TYPES = frozenset({"application/pdf", "image/jpeg", "image/png"})


def _error(
    request: Request,
    status_code: int,
    code: str,
    message: str,
    field_errors: dict[str, list[str]] | None = None,
) -> AuthenticationError:
    return AuthenticationError(
        status_code=status_code,
        detail={
            "code": code,
            "message": message,
            "field_errors": field_errors or {},
            "request_id": getattr(request.state, "request_id", ""),
        },
    )


async def _fetch_idempotency_one(db, statement, **params):
    return (await db.execute(text(statement), params)).mappings().first()


async def _require_own_patient(
    session: AsyncSession,
    *,
    user: User,
    request: Request,
    patient_id: UUID,
) -> None:
    if user.id != patient_id:
        # Hide other patients' OCR existence and metadata.
        raise _error(request, 404, "RESOURCE_NOT_FOUND", "OCR resource not found.")
    source = await require_prescription_access(
        session,
        user=user,
        request=request,
        patient_id=patient_id,
        clinical_write=False,
    )
    if source != "patient":
        raise _error(request, 403, "ACCESS_DENIED", "Permission denied.")


def _job_read(job: PrescriptionOcrJobModel) -> PrescriptionOcrJobRead:
    return PrescriptionOcrJobRead.model_validate(job)


async def create_job(
    *,
    session: AsyncSession,
    user: User,
    request: Request,
    patient_id: UUID,
    payload: PrescriptionOcrJobInput,
    idempotency_key: str,
) -> PrescriptionOcrJobRead:
    try:
        await _require_own_patient(
            session, user=user, request=request, patient_id=patient_id
        )
        document = await session.scalar(
            select(Document)
            .where(
                Document.id == payload.original_document_id,
                Document.owner_user_id == patient_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if document is None:
            raise _error(
                request,
                422,
                "VALIDATION_ERROR",
                "Please select an eligible prescription document.",
                {"original_document_id": ["Select an active prescription document owned by you."]},
            )

        scope = IdempotencyScope(
            "user", user.id, "createPrescriptionOcrJob", str(patient_id), idempotency_key
        )
        digest = request_digest(payload.model_dump(mode="json"))
        try:
            previous = await lock_and_replay(
                session, scope, digest, _fetch_idempotency_one
            )
        except IdempotencyConflict:
            raise _error(
                request,
                409,
                "IDEMPOTENCY_CONFLICT",
                "This key was used with a different request.",
            ) from None
        if previous is not None:
            response = PrescriptionOcrJobRead.model_validate(previous["response_body"])
            await session.commit()
            return response

        if (
            document.patient_user_id not in {None, patient_id}
            or document.document_type != "prescription"
            or document.status != "active"
            or document.media_type not in ALLOWED_MEDIA_TYPES
        ):
            raise _error(
                request,
                422,
                "VALIDATION_ERROR",
                "Please select an eligible prescription document.",
                {
                    "original_document_id": [
                        "Select an active PDF, JPEG or PNG prescription document."
                    ]
                },
            )

        existing = await session.scalar(
            select(PrescriptionOcrJobModel)
            .where(
                PrescriptionOcrJobModel.patient_user_id == patient_id,
                PrescriptionOcrJobModel.original_document_id == document.id,
            )
            .with_for_update()
        )
        if existing is not None:
            raise _error(
                request,
                409,
                "OCR_JOB_ALREADY_EXISTS",
                "An OCR job already exists for this document.",
            )

        job = PrescriptionOcrJobModel(
            patient_user_id=patient_id,
            original_document_id=document.id,
            requested_by_user_id=user.id,
            status="queued",
            attempt_count=0,
        )
        session.add(job)
        await session.flush()
        result = _job_read(job)
        await save_success(session, scope, digest, 202, result.model_dump(mode="json"))
        await append_event(
            session,
            actor_user_id=user.id,
            action="prescription.ocr_job.queued",
            target_type="prescription_ocr_jobs",
            target_id=job.id,
            outcome="success",
            request_id=getattr(request.state, "request_id", ""),
            metadata={"status": "queued"},
        )
        await session.commit()
        return result
    except Exception:
        await session.rollback()
        raise


async def list_jobs(
    *,
    session: AsyncSession,
    user: User,
    request: Request,
    patient_id: UUID,
    page: int,
    page_size: int,
    status_filter: str | None,
) -> PrescriptionOcrJobReadPage:
    await _require_own_patient(session, user=user, request=request, patient_id=patient_id)
    filters = [PrescriptionOcrJobModel.patient_user_id == patient_id]
    if status_filter is not None:
        filters.append(PrescriptionOcrJobModel.status == status_filter)
    total = await session.scalar(
        select(func.count()).select_from(PrescriptionOcrJobModel).where(*filters)
    )
    rows = await session.scalars(
        select(PrescriptionOcrJobModel)
        .where(*filters)
        .order_by(PrescriptionOcrJobModel.created_at.desc(), PrescriptionOcrJobModel.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return PrescriptionOcrJobReadPage(
        items=[_job_read(job) for job in rows.all()],
        page=page,
        page_size=page_size,
        total=int(total or 0),
    )


async def read_job(
    *, session: AsyncSession, user: User, request: Request, job_id: UUID
) -> PrescriptionOcrJobRead:
    job = await session.scalar(
        select(PrescriptionOcrJobModel).where(PrescriptionOcrJobModel.id == job_id)
    )
    if job is None or user.id != job.patient_user_id:
        raise _error(request, 404, "RESOURCE_NOT_FOUND", "OCR resource not found.")
    await _require_own_patient(
        session, user=user, request=request, patient_id=job.patient_user_id
    )
    return _job_read(job)


async def retry_job(
    *,
    session: AsyncSession,
    user: User,
    request: Request,
    job_id: UUID,
    payload: PrescriptionOcrRetryInput,
) -> PrescriptionOcrJobRead:
    try:
        job = await session.scalar(
            select(PrescriptionOcrJobModel)
            .where(PrescriptionOcrJobModel.id == job_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if job is None or user.id != job.patient_user_id:
            raise _error(request, 404, "RESOURCE_NOT_FOUND", "OCR resource not found.")
        await _require_own_patient(
            session, user=user, request=request, patient_id=job.patient_user_id
        )
        if job.status != "failed":
            raise _error(request, 409, "INVALID_STATE", "Only a failed OCR job can be retried.")
        if job.attempt_count >= settings.prescription_ocr_max_attempts:
            raise _error(
                request, 409, "RETRY_LIMIT_REACHED", "The OCR retry limit has been reached."
            )
        job.status = "queued"
        job.error_code = None
        job.updated_at = datetime.now(UTC)
        result = _job_read(job)
        await append_event(
            session,
            actor_user_id=user.id,
            action="prescription.ocr_job.retry_requested",
            target_type="prescription_ocr_jobs",
            target_id=job.id,
            outcome="success",
            request_id=getattr(request.state, "request_id", ""),
            metadata={
                "attempt_count": job.attempt_count,
                "status": "queued",
                "reason_code": "PATIENT_REQUESTED_RETRY",
            },
        )
        # The free-text reason is validated by the API but deliberately not stored
        # in logs or audit metadata because it may contain clinical information.
        _ = payload.reason
        await session.commit()
        return result
    except Exception:
        await session.rollback()
        raise


async def create_reviewed_draft(
    *,
    session: AsyncSession,
    user: User,
    request: Request,
    patient_id: UUID,
    job_id: UUID,
    payload: PrescriptionOcrDraftInput,
    idempotency_key: str,
) -> PrescriptionOcrDraftCreated:
    try:
        job = await session.scalar(
            select(PrescriptionOcrJobModel)
            .where(PrescriptionOcrJobModel.id == job_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if job is None or job.patient_user_id != patient_id or user.id != patient_id:
            raise _error(request, 404, "RESOURCE_NOT_FOUND", "OCR resource not found.")
        await _require_own_patient(session, user=user, request=request, patient_id=patient_id)

        scope = IdempotencyScope(
            "user", user.id, "createPrescriptionOcrDraft", f"{patient_id}:{job_id}", idempotency_key
        )
        digest = request_digest(payload.model_dump(mode="json"))
        try:
            previous = await lock_and_replay(
                session, scope, digest, _fetch_idempotency_one
            )
        except IdempotencyConflict:
            raise _error(
                request,
                409,
                "IDEMPOTENCY_CONFLICT",
                "This key was used with a different request.",
            ) from None
        if previous is not None:
            response = PrescriptionOcrDraftCreated.model_validate(previous["response_body"])
            await session.commit()
            return response

        if job.status != "ready_for_review":
            raise _error(
                request,
                409,
                "INVALID_STATE",
                "The OCR job is not ready for patient review.",
            )

        source = await require_prescription_access(
            session,
            user=user,
            request=request,
            patient_id=patient_id,
            clinical_write=False,
        )
        if source != "patient":
            raise _error(request, 403, "ACCESS_DENIED", "Permission denied.")

        draft_payload = PrescriptionInput(
            consultation_id=None,
            claimed_prescriber=payload.claimed_prescriber,
            starts_on=payload.starts_on,
            ends_on=payload.ends_on,
            original_document_id=job.original_document_id,
            items=payload.items,
        )
        await prescription_service.validate_draft_references(
            session,
            request=request,
            patient_id=patient_id,
            payload=draft_payload,
        )
        prescription = await repository.create_prescription_draft(
            session,
            patient_id=patient_id,
            actor_id=user.id,
            source="patient",
            payload=draft_payload,
        )
        job.status = "draft_created"
        job.resulting_prescription_id = prescription.id
        job.updated_at = datetime.now(UTC)
        prescription_detail: PrescriptionDetail = (
            await prescription_service.build_prescription_detail(session, prescription)
        )
        result = PrescriptionOcrDraftCreated(
            job=_job_read(job), prescription=prescription_detail
        )
        await append_event(
            session,
            actor_user_id=user.id,
            action="prescription.draft.created",
            target_type="prescriptions",
            target_id=prescription.id,
            outcome="success",
            request_id=getattr(request.state, "request_id", ""),
            metadata={"source": "patient", "item_count": len(payload.items)},
        )
        await append_event(
            session,
            actor_user_id=user.id,
            action="prescription.ocr_job.draft_created",
            target_type="prescription_ocr_jobs",
            target_id=job.id,
            outcome="success",
            request_id=getattr(request.state, "request_id", ""),
            metadata={"source": "patient", "status": "draft_created"},
        )
        await save_success(session, scope, digest, 201, result.model_dump(mode="json"))
        await session.commit()
        return result
    except Exception:
        await session.rollback()
        raise
