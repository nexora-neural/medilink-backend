"""Run with python -m app.workers.operations; Ctrl+C stops polling."""

import asyncio

from app.database import models as _models  # noqa: F401
from app.database.session import async_session
from app.modules.operations.service import worker_once


async def main():
    while True:
        try:
            async with async_session() as db:
                processed = await worker_once(db)
        except Exception:
            # No payloads, credentials or raw database exceptions in worker logs.
            print("Operations worker could not complete a request; retrying.", flush=True)
            processed = False
        if not processed:
            await asyncio.sleep(2)


if __name__ == "__main__":
    asyncio.run(main())
