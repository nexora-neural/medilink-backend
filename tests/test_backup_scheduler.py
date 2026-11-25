import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from app.modules.operations import backup_scheduler


def test_scheduled_time_is_utc_and_validates_hour():
    now = datetime(2026, 11, 25, 12, tzinfo=UTC)
    assert backup_scheduler.scheduled_time(now, 2) == datetime(2026, 11, 25, 2, tzinfo=UTC)
    with pytest.raises(ValueError):
        backup_scheduler.scheduled_time(now.replace(tzinfo=None), 2)
    with pytest.raises(ValueError):
        backup_scheduler.scheduled_time(now, 24)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, True])
async def test_due_run_sleeps_until_next_day_or_retries_failure(failure):
    now = datetime(2026, 11, 25, 2, 30, tzinfo=UTC)
    fake_run = AsyncMock(side_effect=RuntimeError if failure else None)
    fake_sleep = AsyncMock(side_effect=asyncio.CancelledError)
    with (
        patch.object(backup_scheduler, "datetime") as clock,
        patch.object(backup_scheduler, "run_backup", fake_run),
        patch.object(backup_scheduler.asyncio, "sleep", fake_sleep),
    ):
        clock.now.return_value = now
        clock.combine.side_effect = datetime.combine
        with pytest.raises(asyncio.CancelledError):
            await backup_scheduler.run_daily(Path("/backup"), Path("/key"), 2)
    fake_run.assert_awaited_once_with(Path("/backup"), Path("/key"), scheduled_date=now.date())
    expected = 900 if failure else timedelta(hours=23, minutes=30).total_seconds()
    fake_sleep.assert_awaited_once_with(expected)


@pytest.mark.asyncio
async def test_before_due_waits_without_starting_backup():
    now = datetime(2026, 11, 25, 1, tzinfo=UTC)
    fake_run = AsyncMock()
    fake_sleep = AsyncMock(side_effect=asyncio.CancelledError)
    with (
        patch.object(backup_scheduler, "datetime") as clock,
        patch.object(backup_scheduler, "run_backup", fake_run),
        patch.object(backup_scheduler.asyncio, "sleep", fake_sleep),
    ):
        clock.now.return_value = now
        clock.combine.side_effect = datetime.combine
        with pytest.raises(asyncio.CancelledError):
            await backup_scheduler.run_daily(Path("/backup"), Path("/key"), 2)
    fake_run.assert_not_awaited()
    fake_sleep.assert_awaited_once_with(3600)
