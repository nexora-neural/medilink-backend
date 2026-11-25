"""Write a snapshot's database stream and verified private files to an encrypted tar stream.

The caller must hold the storage consistency lock and keep the exporting
PostgreSQL transaction alive until the database dump and this function finish.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import stat
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from app.modules.operations.backup_crypto import CHUNK_SIZE, EncryptingWriter

KEY = re.compile(r"[0-9a-f]{32}\Z")


@dataclass(frozen=True)
class DocumentFile:
    key: str
    size: int
    sha256: str


def _entry(archive: tarfile.TarFile, name: str, payload: bytes) -> None:
    item = tarfile.TarInfo(name)
    item.size = len(payload)
    item.mode = 0o600
    archive.addfile(item, io.BytesIO(payload))


def write_bundle(
    writer: EncryptingWriter, dump: BinaryIO, documents: list[DocumentFile], root: Path
) -> None:
    """Write bounded chunks; fail closed on missing, malformed, or changed files."""
    if not hasattr(os, "O_NOFOLLOW"):
        raise OSError("Safe document reads are unavailable")
    if len({doc.key for doc in documents}) != len(documents):
        raise ValueError("Duplicate document storage key")
    with tarfile.open(fileobj=writer, mode="w|") as archive:
        dump_parts = 0
        while chunk := dump.read(CHUNK_SIZE):
            _entry(archive, f"database/{dump_parts:08d}", chunk)
            dump_parts += 1
        if not dump_parts:
            raise ValueError("Database dump is empty")
        file_manifest = []
        for doc in sorted(documents, key=lambda item: item.key):
            if (
                not KEY.fullmatch(doc.key)
                or doc.size <= 0
                or not re.fullmatch(r"[0-9a-fA-F]{64}", doc.sha256 or "")
            ):
                raise ValueError("Invalid document storage metadata")
            fd = os.open(root / doc.key, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                before = os.fstat(fd)
                if not stat.S_ISREG(before.st_mode) or before.st_size != doc.size:
                    raise OSError("Document file does not match stored metadata")
                digest = hashlib.sha256()
                parts = 0
                with os.fdopen(fd, "rb", closefd=False) as stored:
                    remaining = doc.size
                    while remaining:
                        chunk = stored.read(min(CHUNK_SIZE, remaining))
                        if not chunk:
                            raise OSError("Document file was truncated")
                        digest.update(chunk)
                        _entry(archive, f"files/{doc.key}/{parts:08d}", chunk)
                        parts += 1
                        remaining -= len(chunk)
                    if stored.read(1):
                        raise OSError("Document file grew during backup")
                after = os.fstat(fd)
                if (
                    digest.hexdigest() != doc.sha256.lower()
                    or before.st_size != after.st_size
                    or before.st_mtime_ns != after.st_mtime_ns
                    or before.st_ctime_ns != after.st_ctime_ns
                ):
                    raise OSError("Document file changed or failed integrity verification")
                file_manifest.append(
                    {"key": doc.key, "size": doc.size, "sha256": doc.sha256.lower(), "parts": parts}
                )
            finally:
                os.close(fd)
        manifest = {"version": 1, "database_parts": dump_parts, "documents": file_manifest}
        manifest_bytes = json.dumps(manifest, sort_keys=True).encode()
        if len(manifest_bytes) > 1024 * 1024:
            raise ValueError("Backup manifest is too large")
        _entry(archive, "manifest.json", manifest_bytes)
