"""Claim, process and audit queued prescription OCR jobs."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.core.config import settings
from app.modules.audit.service import append_event
from app.modules.documents.models import Document
from app.modules.prescriptions.ocr_engine import OcrProcessingError, extract_document_fields
from app.modules.prescriptions.ocr_template_models import PrescriptionOcrJobModel


async def _record_attempt(
    session: AsyncSession,
    *,
    job: PrescriptionOcrJobModel,
    request_id: str,
    action: str,
    outcome: str,
    code: str | None = None,
) -> None:
    metadata: dict[str, str | int] = {"attempt_count": job.attempt_count}
    if code is not None:
        metadata["code"] = code
    if job.status is not None:
        metadata["status"] = str(job.status)
    await append_event(
        session,
        actor_user_id=job.requested_by_user_id,
        action=action,
        target_type="prescription_ocr_jobs",
        target_id=job.id,
        outcome=outcome,
        request_id=request_id,
        metadata=metadata,
    )


async def _recover_stale_job(session: AsyncSession) -> bool:
    stale_after = timedelta(seconds=settings.prescription_ocr_timeout_seconds + 30)
    stale_before = datetime.now(UTC) - stale_after
    job = await session.scalar(
        select(PrescriptionOcrJobModel)
        .where(
            PrescriptionOcrJobModel.status == "processing",
            PrescriptionOcrJobModel.updated_at <= stale_before,
        )
        .order_by(PrescriptionOcrJobModel.updated_at, PrescriptionOcrJobModel.id)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if job is None:
        return False

    request_id = str(uuid4())
    if job.attempt_count >= settings.prescription_ocr_max_attempts:
        job.status = "failed"
        job.error_code = "OCR_PROCESSING_FAILED"
        await _record_attempt(
            session,
            job=job,
            request_id=request_id,
            action="prescription.ocr_job.attempt.recovered",
            outcome="failed",
            code=job.error_code,
        )
    else:
        # A worker disappeared after its claim. Retain the started-attempt audit
        # entry and put the same job back on the queue for its next bounded try.
        job.status = "queued"
        job.error_code = None
        await _record_attempt(
            session,
            job=job,
            request_id=request_id,
            action="prescription.ocr_job.attempt.recovered",
            outcome="failed",
            code="OCR_PROCESSING_FAILED",
        )
    job.updated_at = datetime.now(UTC)
    await session.commit()
    return True


async def process_one_ocr_job(session: AsyncSession) -> bool:
    """Process one queue item; row locks and status transitions prevent duplicates."""
    try:
        await _recover_stale_job(session)
        job = await session.scalar(
            select(PrescriptionOcrJobModel)
            .where(PrescriptionOcrJobModel.status == "queued")
            .order_by(PrescriptionOcrJobModel.created_at, PrescriptionOcrJobModel.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if job is None:
            await session.rollback()
            return False

        request_id = str(uuid4())
        if job.attempt_count >= settings.prescription_ocr_max_attempts:
            job.status = "failed"
            job.error_code = "OCR_RETRY_LIMIT_REACHED"
            job.updated_at = datetime.now(UTC)
            await _record_attempt(
                session,
                job=job,
                request_id=request_id,
                action="prescription.ocr_job.attempt.completed",
                outcome="failed",
                code=job.error_code,
            )
            await session.commit()
            return True

        document = await session.scalar(
            select(Document).where(
                Document.id == job.original_document_id,
                Document.owner_user_id == job.patient_user_id,
                Document.document_type == "prescription",
                Document.status == "active",
                Document.media_type.in_({"application/pdf", "image/jpeg", "image/png"}),
            )
        )
        job.status = "processing"
        job.attempt_count += 1
        job.error_code = None
        job.updated_at = datetime.now(UTC)
        await _record_attempt(
            session,
            job=job,
            request_id=request_id,
            action="prescription.ocr_job.attempt.started",
            outcome="success",
        )
        await session.commit()

        fields = None
        failure_code = None
        if document is None:
            failure_code = "OCR_PROCESSING_FAILED"
        else:
            try:
                fields = await run_in_threadpool(
                    extract_document_fields,
                    storage_key=document.storage_key,
                    byte_size=document.byte_size,
                    media_type=document.media_type,
                )
            except OcrProcessingError as exc:
                failure_code = exc.code
            except Exception:
                # Do not log engine stderr, input paths, document text or exception
                # details. The public failure is deliberately a stable safe code.
                failure_code = "OCR_PROCESSING_FAILED"

        current = await session.scalar(
            select(PrescriptionOcrJobModel)
            .where(PrescriptionOcrJobModel.id == job.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if current is None or current.status != "processing":
            await session.rollback()
            return True

        current.updated_at = datetime.now(UTC)
        if failure_code is None and fields is not None:
            current.status = "ready_for_review"
            current.extracted_fields = fields
            current.error_code = None
            outcome = "success"
        else:
            current.status = "failed"
            current.extracted_fields = None
            current.error_code = failure_code or "OCR_PROCESSING_FAILED"
            outcome = "failed"
        await _record_attempt(
            session,
            job=current,
            request_id=request_id,
            action="prescription.ocr_job.attempt.completed",
            outcome=outcome,
            code=current.error_code,
        )
        await session.commit()
        return True
    except Exception:
        await session.rollback()
        raise
