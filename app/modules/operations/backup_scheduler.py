"""Standalone UTC daily backup worker; run under a service supervisor."""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime, time, timedelta
from pathlib import Path

from app.modules.operations.backup_runner import run_backup

RETRY_SECONDS = 15 * 60


def scheduled_time(now: datetime, hour: int) -> datetime:
    """Return today's UTC scheduled time for an aware UTC instant."""
    if now.tzinfo is None or now.utcoffset() != timedelta(0) or not 0 <= hour <= 23:
        raise ValueError("Expected UTC time and an hour between 0 and 23")
    return datetime.combine(now.date(), time(hour=hour), tzinfo=UTC)


async def run_daily(directory: Path, key_path: Path, hour: int) -> None:
    """Catch up after startup and retry failures without exposing error details."""
    while True:
        now = datetime.now(UTC)
        due = scheduled_time(now, hour)
        if now < due:
            await asyncio.sleep((due - now).total_seconds())
            continue
        try:
            await run_backup(directory, key_path, scheduled_date=now.date())
        except Exception:
            print("Daily backup failed; retrying in 15 minutes", file=sys.stderr, flush=True)
            await asyncio.sleep(RETRY_SECONDS)
            continue
        now = datetime.now(UTC)
        next_due = scheduled_time(now, hour)
        if next_due <= now:
            next_due += timedelta(days=1)
        await asyncio.sleep((next_due - now).total_seconds())


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the encrypted local backup daily")
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--key-file", type=Path, required=True)
    parser.add_argument("--hour-utc", type=int, default=2, choices=range(24))
    args = parser.parse_args()
    asyncio.run(run_daily(args.directory, args.key_file, args.hour_utc))


if __name__ == "__main__":
    main()
