"""Run one local backup; invoke from a daily system scheduler."""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

import app.database.models  # noqa: F401  # Register all audit foreign-key targets.
from app.database.session import DB_URL, async_session
from app.modules.audit.models import BackupRun, JobRun
from app.modules.audit.service import record_backup_run, record_job_attempt
from app.modules.documents.storage import _root
from app.modules.operations.backup_crypto import read_key
from app.modules.operations.backup_snapshot import capture_backup

LOCK_ID = 0x4D4C313337
lock_engine = create_async_engine(DB_URL, poolclass=NullPool, hide_parameters=True)


def validate_key_location(directory: Path, key_path: Path) -> None:
    """Keep the key outside both the backup set and private document storage."""
    key = key_path.resolve(strict=True)
    backup_root = directory.resolve(strict=True)
    document_root = _root().resolve(strict=True)
    if key == backup_root or backup_root in key.parents:
        raise ValueError("Backup key must be stored separately from backups")
    if key == document_root or document_root in key.parents:
        raise ValueError("Backup key must be stored outside private document storage")


async def recover_interrupted_runs() -> None:
    """Mark our interrupted attempts failed after exclusively acquiring the lock."""
    async with async_session() as session:
        jobs = (
            await session.scalars(
                select(JobRun).where(
                    JobRun.job_type.in_(("local_backup", "local_backup_daily")),
                    JobRun.status == "running",
                    JobRun.job_key.like("local-backup:%"),
                )
            )
        ).all()
        for job in jobs:
            try:
                backup_id = UUID(job.job_key.removeprefix("local-backup:"))
            except ValueError:
                continue
            backup = await session.get(BackupRun, backup_id)
            if backup is None or backup.status != "running":
                continue
            job.status = backup.status = "failed"
            job.finished_at = backup.finished_at = datetime.now(UTC)
            job.last_error_code = backup.error_code = "backup_interrupted"
            await record_job_attempt(session, job, request_id=backup_id.hex, outcome="failed")
            await record_backup_run(session, backup, request_id=backup_id.hex, outcome="failed")
        await session.commit()


async def run_backup(
    directory: Path, key_path: Path, *, scheduled_date: date | None = None
) -> bool:
    """Run once, or once per UTC day when called by the daily scheduler.

    Return False if that day's scheduled backup already succeeded. The daily
    check and run happen under the same PostgreSQL advisory lock.
    """
    async with lock_engine.connect() as lock_connection:
        acquired = await lock_connection.scalar(
            text("SELECT pg_try_advisory_lock(:key)"), {"key": LOCK_ID}
        )
        if not acquired:
            raise RuntimeError("Another backup is already running")
        try:
            await recover_interrupted_runs()
            if scheduled_date is not None:
                if datetime.now(UTC).date() != scheduled_date:
                    return False
                start = datetime.combine(scheduled_date, time.min, tzinfo=UTC)
                async with async_session() as session:
                    already_succeeded = await session.scalar(
                        select(JobRun.id)
                        .where(
                            JobRun.job_type == "local_backup_daily",
                            JobRun.status == "succeeded",
                            JobRun.started_at >= start,
                            JobRun.started_at < start + timedelta(days=1),
                        )
                        .limit(1)
                    )
                    if already_succeeded is not None:
                        return False
            backup_id = uuid4()
            request_id = backup_id.hex
            started = datetime.now(UTC)
            if scheduled_date is not None and started.date() != scheduled_date:
                return False
            async with async_session() as session:
                job = JobRun(
                    job_key=f"local-backup:{backup_id}",
                    job_type="local_backup_daily" if scheduled_date else "local_backup",
                    status="running",
                    attempt_count=1,
                    started_at=started,
                )
                backup = BackupRun(
                    id=backup_id,
                    status="running",
                    database_included=False,
                    files_included=False,
                    started_at=started,
                    restore_check_status="not_run",
                )
                session.add_all([job, backup])
                await session.flush()
                await session.commit()
                job_id, backup_id = job.id, backup.id
            failure = None
            try:
                validate_key_location(directory, key_path)
                key = read_key(key_path)
                await asyncio.to_thread(
                    capture_backup, directory / f"backup-{request_id}.mlbackup", key
                )
            except Exception as exc:
                failure = exc
            async with async_session() as session:
                job = await session.get(JobRun, job_id)
                backup = await session.get(BackupRun, backup_id)
                finished = datetime.now(UTC)
                job.status = backup.status = "failed" if failure else "succeeded"
                job.finished_at = backup.finished_at = finished
                job.last_error_code = backup.error_code = "backup_failed" if failure else None
                backup.database_included = backup.files_included = failure is None
                outcome = "failed" if failure else "success"
                await record_job_attempt(session, job, request_id=request_id, outcome=outcome)
                await record_backup_run(session, backup, request_id=request_id, outcome=outcome)
                await session.commit()
            if failure:
                raise RuntimeError("Backup failed; see the job and backup records") from None
            return True
        finally:
            await lock_connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": LOCK_ID})


def main() -> None:
    parser = argparse.ArgumentParser(description="Create one encrypted local backup")
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--key-file", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(run_backup(args.directory, args.key_file))


if __name__ == "__main__":
    main()
