"""Create a private encrypted archive from one exported PostgreSQL snapshot.

This is a synchronous worker primitive, not a scheduler or an operational
backup runner; it must not run inside the application event loop.
"""

from __future__ import annotations

import asyncio
import os
import re
import stat
import subprocess
import threading
from pathlib import Path
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.database.session import DB_HOST, DB_NAME, DB_PASSWORD, DB_PORT, DB_URL, DB_USER
from app.modules.documents.storage import _root, file_consistency_lock
from app.modules.operations.backup_archive import DocumentFile, write_bundle
from app.modules.operations.backup_crypto import EncryptingWriter, verify_stream

SNAPSHOT = re.compile(r"[0-9A-Fa-f-]+\Z")
PG_DUMP_TIMEOUT_SECONDS = 3600


def _kill_stalled_dump(process, timed_out: threading.Event) -> None:
    if process.poll() is None:
        timed_out.set()
        try:
            process.kill()
        except ProcessLookupError:
            pass


class _TimedDumpReader:
    def __init__(self, stream, timed_out: threading.Event):
        self.stream = stream
        self.timed_out = timed_out

    def read(self, size: int) -> bytes:
        data = self.stream.read(size)
        if self.timed_out.is_set():
            raise TimeoutError("PostgreSQL backup timed out")
        return data


def _pg_environment() -> dict[str, str]:
    # Do not put credentials in command arguments or copy unrelated secrets.
    result = {
        "PATH": os.environ.get("PATH", os.defpath),
        "PGHOST": DB_HOST,
        "PGPORT": str(DB_PORT),
        "PGDATABASE": DB_NAME,
        "PGUSER": DB_USER,
        "PGPASSWORD": DB_PASSWORD,
    }
    return result


async def _capture(destination, key: bytes, root: Path) -> None:
    engine = create_async_engine(DB_URL, poolclass=NullPool, hide_parameters=True)
    try:
        async with engine.connect() as initial:
            connection = await initial.execution_options(isolation_level="REPEATABLE READ")
            async with connection.begin():
                await connection.execute(text("SET TRANSACTION READ ONLY"))
                snapshot = await connection.scalar(text("SELECT pg_export_snapshot()"))
                if not snapshot or not SNAPSHOT.fullmatch(snapshot):
                    raise RuntimeError("Invalid PostgreSQL snapshot identifier")
                rows = await connection.execute(
                    text(
                        "SELECT storage_key, byte_size, sha256 FROM documents ORDER BY storage_key"
                    )
                )
                documents = [
                    DocumentFile(row.storage_key, row.byte_size, row.sha256) for row in rows
                ]
                process = subprocess.Popen(
                    [
                        "pg_dump",
                        "--format=custom",
                        "--no-owner",
                        "--no-privileges",
                        "--no-password",
                        "--snapshot",
                        snapshot,
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    env=_pg_environment(),
                )
                timed_out = threading.Event()
                watchdog = threading.Timer(
                    PG_DUMP_TIMEOUT_SECONDS, _kill_stalled_dump, args=(process, timed_out)
                )
                watchdog.daemon = True
                watchdog.start()
                try:
                    writer = EncryptingWriter(destination, key)
                    assert process.stdout is not None

                    def finish_dump() -> None:
                        if process.wait() != 0 or timed_out.is_set():
                            raise RuntimeError("PostgreSQL backup failed")
                        watchdog.cancel()
                        watchdog.join()

                    write_bundle(
                        writer,
                        _TimedDumpReader(process.stdout, timed_out),
                        documents,
                        root,
                        finish_dump,
                    )
                    writer.finish()
                finally:
                    watchdog.cancel()
                    watchdog.join()
                    if process.stdout is not None:
                        process.stdout.close()
                    if process.poll() is None:
                        process.kill()
                        process.wait()
    finally:
        await engine.dispose()


def capture_backup(destination: Path, key: bytes) -> None:
    """Atomically publish an authenticated backup without overwriting any file."""
    root = _root()
    parent = destination.parent.resolve(strict=True)
    info = parent.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise PermissionError("Backup directory must be private and owned by the current user")
    if parent == root or root in parent.parents:
        raise ValueError("Backup directory must be outside private document storage")
    if not destination.name or destination.name in {".", ".."}:
        raise ValueError("Invalid backup filename")
    destination = parent / destination.name
    temporary = parent / f".backup-{uuid4().hex}.partial"
    fd = os.open(temporary, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w+b") as output:
            with file_consistency_lock(exclusive=False):
                asyncio.run(_capture(output, key, root))
            output.flush()
            os.fsync(output.fileno())
            verify_stream(output, key)
        # A hard link fails if the final name is already in use.
        os.link(temporary, destination, follow_symlinks=False)
        dir_fd = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        temporary.unlink(missing_ok=True)
