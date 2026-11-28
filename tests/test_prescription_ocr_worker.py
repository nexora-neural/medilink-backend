"""Worker concurrency regressions that do not require a database service."""

from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from app.modules.prescriptions.ocr_worker import process_one_ocr_job


class PrescriptionOcrWorkerAttemptTests(IsolatedAsyncioTestCase):
    async def test_stale_worker_discards_result_after_a_new_attempt_is_claimed(self):
        job = SimpleNamespace(
            id=uuid4(),
            status="queued",
            attempt_count=0,
            error_code=None,
            extracted_fields=None,
            updated_at=None,
            requested_by_user_id=uuid4(),
            patient_user_id=uuid4(),
            original_document_id=uuid4(),
        )
        document = SimpleNamespace(
            storage_key="synthetic-document",
            byte_size=1,
            media_type="image/png",
        )
        scalar_results = iter((job, document, job))
        session = SimpleNamespace(
            scalar=AsyncMock(side_effect=lambda _statement: next(scalar_results)),
            commit=AsyncMock(),
            rollback=AsyncMock(),
        )

        async def newer_attempt_finishes_claiming(*_args, **_kwargs):
            # The first OCR call is still in flight, but stale-job recovery
            # has moved the row to a newer processing attempt.
            job.status = "processing"
            job.attempt_count = 2
            return {
                "claimed_prescriber": None,
                "starts_on": None,
                "ends_on": None,
                "items": [],
            }

        with (
            patch(
                "app.modules.prescriptions.ocr_worker._recover_stale_job",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "app.modules.prescriptions.ocr_worker._record_attempt",
                new_callable=AsyncMock,
            ) as record_attempt,
            patch(
                "app.modules.prescriptions.ocr_worker.run_in_threadpool",
                side_effect=newer_attempt_finishes_claiming,
            ),
        ):
            self.assertTrue(await process_one_ocr_job(session))

        self.assertEqual(job.status, "processing")
        self.assertEqual(job.attempt_count, 2)
        self.assertIsNone(job.extracted_fields)
        self.assertEqual(record_attempt.await_count, 1)
        session.rollback.assert_awaited_once()
