"""SQL for deterministic, authorization-filtered pages."""

from sqlalchemy import and_, exists, func, or_, select

from app.modules.audit.models import AuditEvent, BackupRun, JobRun
from app.modules.operations.models import JobRetryRequest
from app.modules.operations.policy import DELEGATED_ACTIONS, patient_predicate
from app.modules.organizations.models import Organization
from app.modules.verification.models import VerificationApplication


async def page_rows(db, model, filters, page, page_size, time_column):
    total = await db.scalar(select(func.count()).select_from(model).where(*filters))
    rows = await db.scalars(
        select(model)
        .where(*filters)
        .order_by(time_column.desc(), model.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return list(rows.all()), total


async def operational_counts(db):
    # Use the same failed-job definition as the filtered jobs list.
    pending_retry = exists(
        select(JobRetryRequest.id).where(
            JobRetryRequest.job_id == JobRun.id,
            JobRetryRequest.status.in_({"queued", "running"}),
        )
    )

    stmt = select(
        select(func.count())
        .select_from(VerificationApplication)
        .where(VerificationApplication.status == "pending")
        .scalar_subquery(),
        select(func.count())
        .select_from(Organization)
        .where(Organization.status == "active")
        .scalar_subquery(),
        select(func.count())
        .select_from(JobRun)
        .where(
            JobRun.status == "failed",
            ~pending_retry,
        )
        .scalar_subquery(),
        select(func.max(BackupRun.finished_at))
        .where(
            BackupRun.status == "succeeded",
            BackupRun.database_included.is_(True),
            BackupRun.files_included.is_(True),
        )
        .scalar_subquery(),
    )

    row = (await db.execute(stmt)).one()

    return dict(
        zip(
            (
                "pending_verifications",
                "active_organizations",
                "failed_jobs",
                "last_backup_at",
            ),
            row,
            strict=True,
        )
    )


async def job_page(db, page, page_size, status):
    pending = exists(
        select(JobRetryRequest.id).where(
            JobRetryRequest.job_id == JobRun.id, JobRetryRequest.status.in_({"queued", "running"})
        )
    )
    if status == "pending":
        filters = [or_(JobRun.status == "queued", and_(JobRun.status == "failed", pending))]
    elif status == "failed":
        filters = [JobRun.status == "failed", ~pending]
    else:
        filters = [JobRun.status == status] if status else []
    return await page_rows(db, JobRun, filters, page, page_size, JobRun.created_at)


async def audit_page(db, roles, page, page_size, action, start, end, request_id):
    filters = [] if "super_admin" in roles else [AuditEvent.action.in_(DELEGATED_ACTIONS)]
    if action is not None:
        filters.append(AuditEvent.action == action)
    if start is not None:
        filters.append(AuditEvent.created_at >= start)
    if end is not None:
        filters.append(AuditEvent.created_at < end)
    if request_id is not None:
        filters.append(AuditEvent.request_id == str(request_id))
    return await page_rows(db, AuditEvent, filters, page, page_size, AuditEvent.created_at)


async def activity_page(db, patient_id, page, page_size):
    return await page_rows(
        db, AuditEvent, [patient_predicate(patient_id)], page, page_size, AuditEvent.created_at
    )


async def backup_page(db, page, page_size):
    return await page_rows(db, BackupRun, [], page, page_size, BackupRun.started_at)


async def lock_job(db, job_id):
    return await db.scalar(select(JobRun).where(JobRun.id == job_id).with_for_update())


async def open_retry(db, job_id):
    return await db.scalar(
        select(JobRetryRequest).where(
            JobRetryRequest.job_id == job_id, JobRetryRequest.status.in_({"queued", "running"})
        )
    )
