"""Run with python -m app.workers.dose_occurrences; Ctrl+C stops polling."""

import asyncio

from app.database import models as _models  # noqa: F401
from app.database.session import async_session
from app.modules.medication.occurrence_worker import process_active_schedules


async def main():
    while True:
        try:
            async with async_session() as session:
                await process_active_schedules(session)
                await session.commit()
        except Exception:
            # Avoid logging medication data, credentials or raw database errors.
            print("Dose-occurrence worker could not complete a pass; retrying.", flush=True)
        await asyncio.sleep(2)


if __name__ == "__main__":
    asyncio.run(main())
