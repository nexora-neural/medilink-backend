"""Local, non-public, random-key file storage (single-server development setup)."""
from __future__ import annotations

import hashlib
import os
import stat
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


def _write(upload: UploadFile) -> tuple[str, int, str]:
    key = uuid4().hex
    target = _path(key)
    size = 0
    digest = hashlib.sha256()
    upload.file.seek(0)
    try:
        with os.fdopen(os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "wb") as out:
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
        target.unlink(missing_ok=True)
        raise
    finally:
        upload.file.seek(0)


async def save(upload: UploadFile) -> tuple[str, int, str]:
    return await run_in_threadpool(_write, upload)


def _delete(key: str) -> None:
    _path(key).unlink(missing_ok=True)


async def delete(key: str) -> None:
    await run_in_threadpool(_delete, key)


def get_download_path(key: str) -> Path:
    path = _path(key)
    mode = path.lstat().st_mode
    if not stat.S_ISREG(mode):
        raise OSError("Stored document is not a regular file")
    return path
