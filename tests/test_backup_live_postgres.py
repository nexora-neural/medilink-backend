"""Opt-in test against two explicitly designated, disposable PostgreSQL databases.

The source must already have project migrations and no users. The target must be
empty. No databases or test data are deleted by this test.
"""

import asyncio
import hashlib
import os
import subprocess
import sys
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import asyncpg
import pytest
from sqlalchemy import select, text

from app.core.config import settings
from app.database.session import async_session, engine
from app.modules.audit.models import AuditEvent, BackupRun, JobRun
from app.modules.documents.storage import file_consistency_lock
from app.modules.operations import backup_runner as runner_module
from app.modules.operations import backup_snapshot
from app.modules.operations.backup_crypto import read_key
from app.modules.operations.backup_restore import prepare_restore
from app.modules.operations.backup_runner import LOCK_ID, run_backup


@pytest.mark.skipif(
    os.environ.get("ML137_LIVE_BACKUP_TEST") != "1", reason="requires isolated test databases"
)
def test_backup_snapshot_excludes_concurrent_commits_and_restores_files():
    source = os.environ["DB_NAME"]
    target = os.environ["ML137_RESTORE_TEST_DATABASE"]
    scheduled_target = os.environ["ML137_SCHEDULED_RESTORE_TEST_DATABASE"]
    assert source.startswith("ml137_test_") and target.startswith("ml137_test_")
    assert scheduled_target.startswith("ml137_test_")
    assert len({source, target, scheduled_target}) == 3
    assert os.environ["DB_HOST"].startswith("/private/tmp")
    connection_args = {
        "host": os.environ["DB_HOST"],
        "port": int(os.environ["DB_PORT"]),
        "user": os.environ["DB_USER"],
    }

    async def check_empty():
        source_conn = await asyncpg.connect(database=source, **connection_args)
        target_conn = await asyncpg.connect(database=target, **connection_args)
        scheduled_conn = await asyncpg.connect(database=scheduled_target, **connection_args)
        try:
            assert await source_conn.fetchval("SELECT count(*) FROM users") == 0
            assert (
                await target_conn.fetchval(
                    "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'"
                )
                == 0
            )
            assert (
                await scheduled_conn.fetchval(
                    "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'"
                )
            ) == 0
        finally:
            await source_conn.close()
            await target_conn.close()
            await scheduled_conn.close()

    asyncio.run(check_empty())
    owner, first, second = uuid4(), uuid4().hex, uuid4().hex
    first_content, second_content = b"synthetic before snapshot", b"synthetic concurrent upload"

    async def insert_document(key, content, create_owner=False):
        conn = await asyncpg.connect(database=source, **connection_args)
        try:
            async with conn.transaction():
                if create_owner:
                    await conn.execute(
                        "INSERT INTO users (id, firebase_uid, display_name) VALUES ($1, $2, $3)",
                        owner,
                        f"synthetic-{owner}",
                        "Synthetic",
                    )
                await conn.execute(
                    "INSERT INTO documents (owner_user_id, source, document_type, "
                    "original_filename, storage_key, media_type, byte_size, sha256) "
                    "VALUES ($1, 'patient', 'report', 'synthetic.pdf', $2, "
                    "'application/pdf', $3, $4)",
                    owner,
                    key,
                    len(content),
                    hashlib.sha256(content).hexdigest(),
                )
        finally:
            await conn.close()

    with tempfile.TemporaryDirectory(prefix="ml137-live-") as directory:
        root = Path(directory)
        storage, backups = root / "storage", root / "backups"
        storage.mkdir(mode=0o700)
        backups.mkdir(mode=0o700)
        (storage / first).write_bytes(first_content)
        asyncio.run(insert_document(first, first_content, create_owner=True))
        snapshot_ready, proceed = threading.Event(), threading.Event()
        real_write_bundle = backup_snapshot.write_bundle
        failures = []

        def paused_write(*args):
            snapshot_ready.set()
            if not proceed.wait(20):
                raise TimeoutError("Concurrent write test timed out")
            return real_write_bundle(*args)

        def capture():
            try:
                backup_snapshot.capture_backup(backups / "live.mlbackup", secret)
            except BaseException as exc:
                failures.append(exc)

        secret = os.urandom(32)
        key_path = root / "key"
        key_path.write_bytes(secret)
        key_path.chmod(0o600)
        with (
            patch.object(settings, "private_upload_dir", str(storage)),
            patch.object(backup_snapshot, "write_bundle", side_effect=paused_write),
        ):
            worker = threading.Thread(target=capture)
            worker.start()
            try:
                assert snapshot_ready.wait(20), "Backup failed before snapshot was ready"
                (storage / second).write_bytes(second_content)
                asyncio.run(insert_document(second, second_content))
            finally:
                proceed.set()
                worker.join(timeout=30)
            assert not worker.is_alive()
            assert not failures, failures

        stage = prepare_restore(backups / "live.mlbackup", read_key(key_path), root)
        assert (stage / "files" / first).read_bytes() == first_content
        assert not (stage / "files" / second).exists()
        env = {
            "PATH": os.environ.get("PATH", os.defpath),
            "PGHOST": connection_args["host"],
            "PGPORT": str(connection_args["port"]),
            "PGUSER": connection_args["user"],
        }
        subprocess.run(
            [
                "pg_restore",
                "--exit-on-error",
                "--no-owner",
                "--no-privileges",
                "--dbname",
                target,
                str(stage / "database.dump"),
            ],
            env=env,
            check=True,
            timeout=60,
        )

        async def restored_keys():
            conn = await asyncpg.connect(database=target, **connection_args)
            try:
                return set(await conn.fetch("SELECT storage_key FROM documents"))
            finally:
                await conn.close()

        rows = asyncio.run(restored_keys())
        assert {row["storage_key"] for row in rows} == {first}

        async def exercise_runner():
            # The same database cannot start two backups at once.
            async with engine.connect() as lock:
                await lock.execute(text("SELECT pg_advisory_lock(:key)"), {"key": LOCK_ID})
                try:
                    with pytest.raises(RuntimeError, match="already running"):
                        await run_backup(backups, key_path)
                finally:
                    await lock.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": LOCK_ID})

            interrupted_id = uuid4()
            async with async_session() as session:
                session.add_all(
                    [
                        JobRun(
                            job_key=f"local-backup:{interrupted_id}",
                            job_type="local_backup",
                            status="running",
                            attempt_count=1,
                        ),
                        BackupRun(
                            id=interrupted_id,
                            status="running",
                            database_included=False,
                            files_included=False,
                            restore_check_status="not_run",
                        ),
                    ]
                )
                await session.commit()
            capture_started, capture_continue = threading.Event(), threading.Event()
            real_capture = runner_module.capture_backup

            def paused_capture(destination, secret):
                capture_started.set()
                if not capture_continue.wait(20):
                    raise TimeoutError("Backup runner test timed out")
                real_capture(destination, secret)

            with patch.object(runner_module, "capture_backup", side_effect=paused_capture):
                task = asyncio.create_task(run_backup(backups, key_path))
                try:
                    assert await asyncio.to_thread(capture_started.wait, 20)
                    async with async_session() as session:
                        running = (
                            await session.scalars(
                                select(BackupRun).where(BackupRun.status == "running")
                            )
                        ).all()
                        assert len(running) == 1
                        assert not running[0].database_included and not running[0].files_included
                        events = (
                            await session.scalars(
                                select(AuditEvent).where(AuditEvent.target_id == running[0].id)
                            )
                        ).all()
                        assert not any(event.outcome == "success" for event in events)
                finally:
                    capture_continue.set()
                    await task
            with pytest.raises(RuntimeError, match="Backup failed"):
                await run_backup(backups, root / "missing-key")
            real_popen = subprocess.Popen
            for program in (
                "import time; time.sleep(60)",
                "import os,time; os.write(1,b'partial'); os.close(1); time.sleep(60)",
            ):
                children = []

                def stalled_dump(_command, **kwargs):
                    child = real_popen([sys.executable, "-c", program], **kwargs)
                    children.append(child)
                    return child

                async with async_session() as session:
                    failed_before = len(
                        (
                            await session.scalars(
                                select(BackupRun).where(BackupRun.status == "failed")
                            )
                        ).all()
                    )
                existing_backups = set(backups.iterdir())
                with (
                    patch.object(backup_snapshot.subprocess, "Popen", side_effect=stalled_dump),
                    patch.object(backup_snapshot, "PG_DUMP_TIMEOUT_SECONDS", 0.2),
                ):
                    with pytest.raises(RuntimeError, match="Backup failed"):
                        await asyncio.wait_for(run_backup(backups, key_path), timeout=10)
                assert children and children[0].poll() is not None
                assert set(backups.iterdir()) == existing_backups
                with file_consistency_lock(exclusive=True):
                    pass
                async with async_session() as session:
                    failed_after = len(
                        (
                            await session.scalars(
                                select(BackupRun).where(BackupRun.status == "failed")
                            )
                        ).all()
                    )
                    assert failed_after == failed_before + 1
            today = datetime.now(UTC).date()
            with pytest.raises(RuntimeError, match="Backup failed"):
                await run_backup(backups, root / "missing-key", scheduled_date=today)
            existing_backups = set(backups.iterdir())
            assert await run_backup(backups, key_path, scheduled_date=today)
            (scheduled_backup,) = set(backups.iterdir()) - existing_backups
            assert not await run_backup(backups, key_path, scheduled_date=today)
            scheduled_stage = prepare_restore(scheduled_backup, read_key(key_path), root)
            assert (scheduled_stage / "files" / first).read_bytes() == first_content
            assert (scheduled_stage / "files" / second).read_bytes() == second_content
            subprocess.run(
                [
                    "pg_restore",
                    "--exit-on-error",
                    "--no-owner",
                    "--no-privileges",
                    "--dbname",
                    scheduled_target,
                    str(scheduled_stage / "database.dump"),
                ],
                env=env,
                check=True,
                timeout=60,
            )
            conn = await asyncpg.connect(database=scheduled_target, **connection_args)
            try:
                restored = await conn.fetch("SELECT storage_key FROM documents")
                assert {row["storage_key"] for row in restored} == {first, second}
            finally:
                await conn.close()

            async with async_session() as session:
                interrupted = await session.get(BackupRun, interrupted_id)
                assert interrupted.status == "failed"
                assert interrupted.error_code == "backup_interrupted"
                records = (await session.scalars(select(BackupRun))).all()
                assert any(
                    run.status == "succeeded"
                    and run.database_included
                    and run.files_included
                    and run.restore_check_status == "not_run"
                    for run in records
                )
                assert any(
                    run.status == "failed"
                    and not run.database_included
                    and not run.files_included
                    and run.error_code == "backup_failed"
                    for run in records
                )
                events = (await session.scalars(select(AuditEvent))).all()
                assert any(
                    event.action == "backup.run"
                    and event.target_id == interrupted_id
                    and event.outcome == "failed"
                    for event in events
                )
                daily_jobs = (
                    await session.scalars(
                        select(JobRun).where(JobRun.job_type == "local_backup_daily")
                    )
                ).all()
                assert len(daily_jobs) == 2
                assert sorted(job.status for job in daily_jobs) == ["failed", "succeeded"]
            await engine.dispose()

        with patch.object(settings, "private_upload_dir", str(storage)):
            asyncio.run(exercise_runner())
