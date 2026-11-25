"""Local, non-public, random-key file storage (single-server development setup)."""
from __future__ import annotations

import fcntl
import hashlib
import os
import stat
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from fastapi import UploadFile
from starlette.concurrency import run_in_threadpool

from app.core.config import settings

CHUNK_SIZE = 64 * 1024


def _root() -> Path:
    configured = Path(settings.private_upload_dir).absolute()
    configured.mkdir(mode=0o700, parents=True, exist_ok=True)
    if configured.is_symlink():
        raise OSError("Private storage directory must not be a symlink")
    root = configured.resolve()
    if not root.is_dir():
        raise OSError("Invalid private storage directory")
    return root


def _path(key: str) -> Path:
    if len(key) != 32 or any(c not in "0123456789abcdef" for c in key):
        raise ValueError("Invalid storage key")
    return _root() / key


@contextmanager
def file_consistency_lock(*, exclusive: bool):
    """Coordinate local backups with operations that remove private files.

    All processes must use the same private storage root on a filesystem with
    working advisory flock support. The lock file must never be removed.
    """
    if not hasattr(os, "O_NOFOLLOW"):
        raise OSError("Safe private-storage locking is unavailable")
    path = _root() / ".backup-consistency.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
            raise OSError("Invalid private-storage lock")
        if info.st_mode & 0o077:
            raise PermissionError("Private-storage lock is not private")
        fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _write(upload: UploadFile) -> tuple[str, int, str]:
    key = uuid4().hex
    target = _path(key)
    size = 0
    digest = hashlib.sha256()
    created = False
    upload.file.seek(0)
    try:
        fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        created = True
        with os.fdopen(fd, "wb") as out:
            while block := upload.file.read(CHUNK_SIZE):
                size += len(block)
                if size > 10_485_760:
                    raise ValueError("File size limit exceeded")
                digest.update(block)
                out.write(block)
        if size == 0:
            raise ValueError("Empty file")
        return key, size, digest.hexdigest()
    except Exception:
        if created:
            _delete(key)
        raise
    finally:
        upload.file.seek(0)


async def save(upload: UploadFile) -> tuple[str, int, str]:
    return await run_in_threadpool(_write, upload)


def _delete(key: str) -> None:
    with file_consistency_lock(exclusive=True):
        _path(key).unlink(missing_ok=True)


async def delete(key: str) -> None:
    await run_in_threadpool(_delete, key)


def get_download_path(key: str, expected_size: int | None = None) -> Path:
    path = _path(key)
    # Opening also detects unreadable files; lstat alone only proves existence.
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or path.is_symlink():
            raise OSError("Stored document is not a regular file")
        if expected_size is not None and info.st_size != expected_size:
            raise OSError("Stored document size does not match metadata")
    finally:
        os.close(fd)
    return path
