"""Offline inventory and quarantine for private-storage inconsistencies.

Run only while all application writers and other reconciliation jobs are stopped.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import stat
import time
from pathlib import Path

from sqlalchemy import text

from app.database.session import async_session, engine
from app.modules.documents.storage import _root

KEY = re.compile(r"[0-9a-f]{32}\Z")
MIN_AGE_SECONDS = 3600


def inventory(root: Path, references: dict[str, str], now: float) -> tuple[list[str], list[str]]:
    """List old orphan keys and document IDs with missing/non-regular files."""
    orphans = []
    for item in root.iterdir():
        if not KEY.fullmatch(item.name):
            continue
        info = item.lstat()
        if (
            stat.S_ISREG(info.st_mode)
            and item.name not in references
            and now - info.st_mtime >= MIN_AGE_SECONDS
        ):
            orphans.append(item.name)
    missing = []
    for key, document_id in references.items():
        if not KEY.fullmatch(key):
            missing.append(document_id)
            continue
        try:
            info = (root / key).lstat()
            if not stat.S_ISREG(info.st_mode):
                missing.append(document_id)
        except FileNotFoundError:
            missing.append(document_id)
    return sorted(orphans), sorted(missing)


def quarantine(root: Path, keys: list[str]) -> None:
    """Move orphan files without overwriting an existing quarantine entry."""
    destination = root / ".quarantine"
    destination.mkdir(mode=0o700, exist_ok=True)
    if destination.is_symlink() or not destination.is_dir():
        raise OSError("Invalid quarantine directory")
    os.chmod(destination, 0o700)
    for key in keys:
        if not KEY.fullmatch(key):
            raise ValueError("Invalid storage key")
        source = root / key
        if not stat.S_ISREG(source.lstat().st_mode):
            raise OSError("Orphan is no longer a regular file")
        target = destination / key
        try:
            # Never overwrite an existing quarantine entry.
            os.link(source, target, follow_symlinks=False)
        except FileExistsError:
            source_stat = source.lstat()
            target_stat = target.lstat()
            if (
                not stat.S_ISREG(target_stat.st_mode)
                or (source_stat.st_dev, source_stat.st_ino)
                != (target_stat.st_dev, target_stat.st_ino)
            ):
                raise
        source.unlink()


async def run(apply: bool) -> int:
    root = _root()
    async with async_session() as session:
        result = await session.execute(text("SELECT storage_key, id FROM documents"))
        references = {row.storage_key: str(row.id) for row in result}
        orphans, missing = inventory(root, references, time.time())
        print(f"Orphan candidates: {len(orphans)}; missing document files: {len(missing)}")
        for key in orphans:
            print(f"orphan {key}")
        for document_id in missing:
            print(f"missing document {document_id}")
        if apply:
            quarantine(root, orphans)
            print(f"Quarantined {len(orphans)} orphan files")
    await engine.dispose()
    return 1 if missing else 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quarantine-orphans", action="store_true")
    parser.add_argument("--writers-stopped", action="store_true")
    args = parser.parse_args()
    if not args.writers_stopped:
        parser.error("stop all writers and pass --writers-stopped")
    raise SystemExit(asyncio.run(run(args.quarantine_orphans)))


if __name__ == "__main__":
    main()
