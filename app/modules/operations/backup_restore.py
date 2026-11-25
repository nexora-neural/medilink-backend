"""Authenticate and validate a backup into a new, private restore staging area."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import tarfile
import tempfile
from pathlib import Path

from app.modules.operations.backup_crypto import CHUNK_SIZE, decrypt_to_staging, read_key

_DATABASE = re.compile(r"database/([0-9]{8})\Z")
_FILE = re.compile(r"files/([0-9a-f]{32})/([0-9]{8})\Z")


def prepare_restore(backup: Path, key: bytes, staging_parent: Path) -> Path:
    """Return a newly created staging directory; never touch an existing target.

    Authentication, archive structure, document lengths and hashes must all
    succeed before the caller may use the staged dump or files.
    """
    info = staging_parent.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise PermissionError("Restore staging parent must be an owned private directory")
    stage = Path(tempfile.mkdtemp(prefix="medilink-restore-", dir=staging_parent))
    try:
        (stage / "files").mkdir(mode=0o700)
        with backup.open("rb") as source, tempfile.TemporaryFile(dir=stage) as plaintext:
            decrypt_to_staging(source, plaintext, key)
            plaintext.seek(0)
            _unpack(plaintext, stage)
        return stage
    except BaseException:
        shutil.rmtree(stage)
        raise


def _unpack(source, stage: Path) -> None:
    database_parts = 0
    documents = {}
    manifest = None
    last_key = ""
    with (
        (stage / "database.dump").open("xb") as dump,
        tarfile.open(fileobj=source, mode="r:") as archive,
    ):
        os.chmod(stage / "database.dump", 0o600)
        for item in archive:
            if not item.isfile() or manifest is not None:
                raise ValueError("Invalid backup member or ordering")
            if item.name == "manifest.json":
                if item.size > 1024 * 1024:
                    raise ValueError("Backup manifest is too large")
                stream = archive.extractfile(item)
                manifest = json.loads(stream.read())
                continue
            if item.size <= 0 or item.size > CHUNK_SIZE:
                raise ValueError("Invalid backup chunk size")
            stream = archive.extractfile(item)
            chunk = stream.read()
            if len(chunk) != item.size:
                raise ValueError("Truncated backup chunk")
            db_match = _DATABASE.fullmatch(item.name)
            file_match = _FILE.fullmatch(item.name)
            if db_match:
                if documents or int(db_match.group(1)) != database_parts:
                    raise ValueError("Invalid database chunk ordering")
                dump.write(chunk)
                database_parts += 1
            elif file_match:
                key, part = file_match.groups()
                if not database_parts or key < last_key:
                    raise ValueError("Invalid document chunk ordering")
                last_key = key
                entry = documents.setdefault(key, {"size": 0, "parts": 0, "hash": hashlib.sha256()})
                if int(part) != entry["parts"]:
                    raise ValueError("Invalid document chunk ordering")
                path = stage / "files" / key
                with path.open("xb" if entry["parts"] == 0 else "ab") as target:
                    os.chmod(path, 0o600)
                    target.write(chunk)
                entry["size"] += len(chunk)
                entry["parts"] += 1
                entry["hash"].update(chunk)
            else:
                raise ValueError("Unexpected backup member")
    if not database_parts or not isinstance(manifest, dict):
        raise ValueError("Backup manifest or database is missing")
    expected = [
        {
            "key": key,
            "size": value["size"],
            "parts": value["parts"],
            "sha256": value["hash"].hexdigest(),
        }
        for key, value in documents.items()
    ]
    if manifest != {"version": 1, "database_parts": database_parts, "documents": expected}:
        raise ValueError("Backup manifest does not match its contents")


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare an authenticated backup for restore")
    parser.add_argument("--backup", required=True, type=Path)
    parser.add_argument("--key-file", required=True, type=Path)
    parser.add_argument("--staging-parent", required=True, type=Path)
    args = parser.parse_args()
    print(prepare_restore(args.backup, read_key(args.key_file), args.staging_parent))


if __name__ == "__main__":
    main()
