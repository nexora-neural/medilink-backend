"""ML-67 permission checks, safe projections and transactional retry scheduling."""

from datetime import UTC, datetime
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select

from app.modules.audit.service import append_event
from app.modules.identity.authorization import require_role
from app.modules.identity.models import User
from app.modules.identity.service import SupportDecisionError, authorize_account_support
from app.modules.operations import repository as repo
from app.modules.operations.jobs import HANDLERS
from app.modules.operations.models import JobRetryRequest
from app.modules.operations.policy import safe_metadata
from app.modules.operations.schemas import (
    AuditEventsReadPage,
    AuditRead,
    BackupPage,
    BackupRead,
    JobStatus,
    JobStatusPage,
    OperationalSummary,
    PatientActivity,
    PatientActivityPage,
)
from app.modules.profiles.models import PatientProfile


async def require_admin(db, actor):
    try:
        return await authorize_account_support(db, actor.id)
    except SupportDecisionError as exc:
        raise HTTPException(exc.status, str(exc)) from None


def safe_error(code):
    # Never echo untrusted error strings from worker/provider rows.
    allowed = {
        "JOB_FAILED",
        "RETRY_FAILED",
        "ACCESS_REVOKED",
        "RETRY_LIMIT_REACHED",
        "UNSUPPORTED_JOB_TYPE",
        "BACKUP_FAILED",
        "RESTORE_FAILED",
    }
    return code if code in allowed else "JOB_FAILED" if code else None


def job_read(job, pending=None):
    timestamps = [value for value in (job.created_at, job.started_at, job.finished_at) if value]
    if pending is not None and getattr(pending, "created_at", None) is not None:
        timestamps.append(pending.created_at)
    return JobStatus(
        id=job.id,
        job_type=job.job_type,
        status="pending" if job.status == "queued" or pending is not None else job.status,
        attempt_count=job.attempt_count,
        error_code=safe_error(job.last_error_code),
        updated_at=max(timestamps),
    )


async def summary(db, actor):
    await require_admin(db, actor)
    return OperationalSummary(**await repo.operational_counts(db))


async def jobs(db, actor, page, page_size, status):
    await require_admin(db, actor)
    rows, total = await repo.job_page(db, page, page_size, status)
    return JobStatusPage(
        items=[job_read(row, await repo.open_retry(db, row.id)) for row in rows],
        page=page,
        page_size=page_size,
        total=total,
    )


async def audit(db, actor, page, page_size, action, start, end, request_id):
    roles = await require_admin(db, actor)
    for value in (start, end):
        if value is not None and value.utcoffset() is None:
            raise HTTPException(422, "Audit timestamps must include a timezone.")
    if start is not None and end is not None and start >= end:
        raise HTTPException(422, "from must be earlier than to.")
    rows, total = await repo.audit_page(db, roles, page, page_size, action, start, end, request_id)
    return AuditEventsReadPage(
        items=[
            AuditRead(
                id=row.id,
                actor_user_id=row.actor_user_id,
                actor_client_id=row.actor_client_id,
                action=row.action,
                target_type=row.target_type,
                target_id=row.target_id,
                outcome=row.outcome,
                request_id=row.request_id,
                metadata=safe_metadata(row.metadata_),
                created_at=row.created_at,
            )
            for row in rows
        ],
        page=page,
        page_size=page_size,
        total=total,
    )


async def activity(db, actor, patient_id, page, page_size):
    await require_role(db, actor, "patient")
    if actor.id != patient_id:
        raise HTTPException(404, "Patient activity not found.")
    if (
        await db.scalar(select(PatientProfile.user_id).where(PatientProfile.user_id == actor.id))
        is None
    ):
        raise HTTPException(404, "Patient activity not found.")
    rows, total = await repo.activity_page(db, patient_id, page, page_size)
    return PatientActivityPage(
        items=[
            PatientActivity(
                id=row.id,
                action=row.action,
                target_type=row.target_type,
                target_id=row.target_id,
                outcome=row.outcome,
                created_at=row.created_at,
            )
            for row in rows
        ],
        page=page,
        page_size=page_size,
        total=total,
    )


async def backups(db, actor, page, page_size):
    await require_admin(db, actor)
    rows, total = await repo.backup_page(db, page, page_size)
    return BackupPage(
        items=[
            BackupRead(
                id=row.id,
                status=row.status,
                database_included=row.database_included,
                files_included=row.files_included,
                started_at=row.started_at,
                finished_at=row.finished_at,
                restore_check_status=row.restore_check_status,
                restore_checked_at=row.restore_checked_at,
                error_code=(
                    row.error_code
                    if row.error_code in {"BACKUP_FAILED", "RESTORE_FAILED"}
                    else "BACKUP_FAILED"
                    if row.error_code
                    else None
                ),
            )
            for row in rows
        ],
        page=page,
        page_size=page_size,
        total=total,
    )


async def retry(db, actor, job_id: UUID, reason, request_id):
    await require_admin(db, actor)
    try:
        job = await repo.lock_job(db, job_id)
        if job is None:
            raise HTTPException(404, "Job not found.")
        existing = await repo.open_retry(db, job_id)
        if existing is not None:
            # State-idempotence: concurrent/repeated submissions cannot enqueue twice.
            if existing.requested_by != actor.id:
                raise HTTPException(409, "A retry is already scheduled.")
            return job_read(job, existing)
        if job.status != "failed":
            raise HTTPException(409, "Only a failed job can be retried.")
        handler = HANDLERS.get(job.job_type)
        if handler is None:
            raise HTTPException(409, "This job type does not support safe retries.")
        if job.attempt_count >= handler.max_attempts:
            raise HTTPException(409, "Retry limit reached.")
        if not await handler.authorize(db, job, actor.id):
            raise HTTPException(403, "Job retry permission denied.")
        item = JobRetryRequest(
            job_id=job.id,
            requested_by=actor.id,
            reason=reason,
            request_id=request_id,
            attempt_number=job.attempt_count + 1,
            status="queued",
            created_at=datetime.now(UTC),
        )
        db.add(item)
        # Keep the previous job status/error/timestamps intact until an actual attempt.
        await append_event(
            db,
            actor_user_id=actor.id,
            action="job.retry.requested",
            target_type="job_run",
            target_id=job.id,
            outcome="success",
            request_id=request_id,
            metadata={"job_type": job.job_type, "attempt_count": job.attempt_count},
        )
        await db.commit()
        return job_read(job, item)
    except Exception:
        await db.rollback()
        raise


async def worker_once(db):
    """Consume one durable request. Row locks prevent duplicate execution.

    A worker crash rolls the transaction back, leaving the request queued. All
    registered handlers must be transactional; external effects need idempotency.
    """
    item = await db.scalar(
        select(JobRetryRequest)
        .where(JobRetryRequest.status == "queued")
        .order_by(JobRetryRequest.created_at, JobRetryRequest.id)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if item is None:
        await db.rollback()
        return False
    job = await repo.lock_job(db, item.job_id)
    actor = await db.get(User, item.requested_by, populate_existing=True)
    handler = HANDLERS.get(job.job_type)
    try:
        await require_admin(db, actor)
        allowed = handler is not None and await handler.authorize(db, job, item.requested_by)
    except (HTTPException, AttributeError):
        allowed = False
    if (
        not allowed
        or job.status != "failed"
        or job.attempt_count >= (handler.max_attempts if handler else 0)
    ):
        item.status = "cancelled"
        item.finished_at = datetime.now(UTC)
        await append_event(
            db,
            actor_user_id=item.requested_by,
            action="job.retry.cancelled",
            target_type="job_run",
            target_id=job.id,
            outcome="denied",
            request_id=item.request_id,
            metadata={"code": "ACCESS_REVOKED" if not allowed else "INVALID_STATE"},
        )
        await db.commit()
        return True
    item.status = "running"
    job.status = "running"
    job.attempt_count += 1
    job.started_at = datetime.now(UTC)
    try:
        async with db.begin_nested():
            await handler.run(db, job, item.requested_by)
            await db.flush()
    except Exception:
        # Savepoint rolls back handler work. Never persist exception strings.
        job.status = item.status = "failed"
        job.last_error_code = "RETRY_FAILED"
    else:
        job.status = item.status = "succeeded"
        job.last_error_code = None
    job.finished_at = item.finished_at = datetime.now(UTC)
    await append_event(
        db,
        actor_user_id=item.requested_by,
        action="job.retry.completed",
        target_type="job_run",
        target_id=job.id,
        outcome="success" if job.status == "succeeded" else "failed",
        request_id=item.request_id,
        metadata={
            "job_type": job.job_type,
            "attempt_count": job.attempt_count,
            "status": job.status,
        },
    )
    await db.commit()
    return True
