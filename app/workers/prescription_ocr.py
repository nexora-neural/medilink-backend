"""Run with python -m app.workers.prescription_ocr; Ctrl+C stops polling."""

import asyncio

from app.database import models as _models  # noqa: F401
from app.database.session import async_session
from app.modules.prescriptions.ocr_worker import process_one_ocr_job


async def main() -> None:
    while True:
        try:
            async with async_session() as session:
                processed = await process_one_ocr_job(session)
        except Exception:
            # Keep OCR payloads and engine diagnostics out of service logs.
            print("Prescription OCR worker could not complete a job; retrying.", flush=True)
            processed = False
        if not processed:
            await asyncio.sleep(2)


if __name__ == "__main__":
    asyncio.run(main())
