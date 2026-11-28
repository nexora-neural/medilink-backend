"""Synthetic PostgreSQL and API-to-worker OCR workflow verification."""

import hashlib
import os
import tempfile
import unittest
from asyncio import Event, create_task, gather, sleep
from datetime import date
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from httpx import ASGITransport, AsyncClient
from PIL import Image, ImageDraw, ImageFont
from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.config import settings
from app.core.idempotency import IdempotencyScope, lock_and_replay, request_digest, save_success
from app.database import models as _models  # noqa: F401
from app.database.session import get_session
from app.main import app
from app.modules.audit.models import ApiIdempotencyRecord, AuditEvent
from app.modules.documents.models import Document
from app.modules.identity.auth import get_current_user
from app.modules.identity.models import Role, User, UserRole
from app.modules.identity.request_limits import (
    require_authenticated_rate_limit,
    require_upload_rate_limit,
)
from app.modules.prescriptions.core_models import Prescription
from app.modules.prescriptions.ocr_template_models import PrescriptionOcrJobModel
from app.modules.prescriptions.ocr_worker import process_one_ocr_job
from app.modules.profiles.models import PatientProfile

TEST_DATABASE_URL = os.getenv("PRESCRIPTION_OCR_TEST_DATABASE_URL")


def _synthetic_page():
    candidates = (
        Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    )
    font_path = next((path for path in candidates if path.is_file()), None)
    font = ImageFont.truetype(str(font_path), 42) if font_path else ImageFont.load_default(size=42)
    image = Image.new("RGB", (1700, 1200), "white")
    draw = ImageDraw.Draw(image)
    lines = (
        "MEDILINK SYNTHETIC PRESCRIPTION",
        "Prescriber: Dr. Jane Example",
        "Start date: 2026-11-28",
        "End date: 2026-12-02",
        "Amoxicillin 500 mg - Take 1 capsule three times a day for 5 days",
    )
    for number, line in enumerate(lines):
        draw.text((90, 100 + number * 150), line, fill="black", font=font)
    return image


@unittest.skipUnless(
    TEST_DATABASE_URL,
    "Set PRESCRIPTION_OCR_TEST_DATABASE_URL to an isolated migrated PostgreSQL database",
)
class PrescriptionOcrPostgresWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine(TEST_DATABASE_URL)
        self.connection = await self.engine.connect()
        self.transaction = await self.connection.begin()
        self.db = AsyncSession(
            bind=self.connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        self.addAsyncCleanup(self._cleanup)
        self.old_private_dir = settings.private_upload_dir
        self.temp_storage = tempfile.TemporaryDirectory(prefix="ml152-private-storage-")
        settings.private_upload_dir = self.temp_storage.name
        Path(settings.private_upload_dir).chmod(0o700)

        self.patient = User(
            firebase_uid=f"ml152-{uuid4()}",
            display_name="Synthetic OCR patient",
            status="active",
        )
        self.db.add(self.patient)
        await self.db.flush()
        self.patient_id = self.patient.id
        self.db.add(
            PatientProfile(
                user_id=self.patient_id,
                date_of_birth=date(1990, 1, 1),
                sex="female",
            )
        )
        patient_role = await self.db.scalar(select(Role).where(Role.code == "patient"))
        if patient_role is None:
            patient_role = Role(code="patient", label="Patient")
            self.db.add(patient_role)
            await self.db.flush()
        self.db.add(UserRole(user_id=self.patient_id, role_id=patient_role.id))
        await self.db.flush()

        self.document_id, self.storage_key, self.original_bytes = await self._add_document(
            valid=True, extension="png", media_type="image/png"
        )
        self.bad_document_id, self.bad_storage_key, self.bad_bytes = await self._add_document(
            valid=False, extension="png", media_type="image/png"
        )
        await self.db.commit()

        self.previous_overrides = app.dependency_overrides.copy()

        async def db_override():
            async with AsyncSession(
                bind=self.connection,
                expire_on_commit=False,
                join_transaction_mode="create_savepoint",
            ) as session:
                yield session

        async def no_rate_limit():
            return None

        app.dependency_overrides.clear()
        app.dependency_overrides.update(
            {
                get_session: db_override,
                get_current_user: lambda: self.patient,
                require_authenticated_rate_limit: no_rate_limit,
                require_upload_rate_limit: no_rate_limit,
            }
        )
        self.client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

    async def _cleanup(self):
        if hasattr(self, "client"):
            await self.client.aclose()
        app.dependency_overrides.clear()
        app.dependency_overrides.update(getattr(self, "previous_overrides", {}))
        if hasattr(self, "db"):
            await self.db.close()
        if hasattr(self, "transaction") and self.transaction.is_active:
            await self.transaction.rollback()
        if hasattr(self, "connection"):
            await self.connection.close()
        if hasattr(self, "engine"):
            await self.engine.dispose()
        if hasattr(self, "temp_storage"):
            settings.private_upload_dir = self.old_private_dir
            self.temp_storage.cleanup()

    async def _add_document(self, *, valid: bool, extension: str, media_type: str):
        key = uuid4().hex
        path = Path(settings.private_upload_dir) / key
        if valid:
            _synthetic_page().save(path, format=extension.upper())
        else:
            path.write_bytes(b"not a valid PNG; synthetic OCR failure")
        raw = path.read_bytes()
        document = Document(
            owner_user_id=self.patient_id,
            patient_user_id=self.patient_id,
            source="patient",
            document_type="prescription",
            original_filename=f"synthetic-prescription.{extension}",
            storage_key=key,
            media_type=media_type,
            byte_size=len(raw),
            sha256=hashlib.sha256(raw).hexdigest(),
            status="active",
        )
        self.db.add(document)
        await self.db.flush()
        return document.id, key, raw

    async def test_ocr_api_worker_reviewed_draft_confirmation_and_history(self):
        jobs_url = f"/api/v1/patients/{self.patient_id}/prescription-ocr-jobs"
        created = await self.client.post(
            jobs_url,
            json={"original_document_id": str(self.document_id)},
            headers={"Idempotency-Key": "ml152-postgres-create-1"},
        )
        self.assertEqual(created.status_code, 202, created.text)
        job_id = created.json()["id"]
        self.assertEqual(created.json()["status"], "queued")

        replay = await self.client.post(
            jobs_url,
            json={"original_document_id": str(self.document_id)},
            headers={"Idempotency-Key": "ml152-postgres-create-1"},
        )
        self.assertEqual(replay.status_code, 202, replay.text)
        self.assertEqual(replay.json()["id"], job_id)
        self.assertEqual(replay.json(), created.json())

        listing = await self.client.get(jobs_url, params={"status": "queued", "page": 1})
        self.assertEqual((listing.status_code, listing.json()["total"]), (200, 1))
        detail = await self.client.get(f"/api/v1/prescription-ocr-jobs/{job_id}")
        self.assertEqual(detail.json()["status"], "queued")

        self.assertTrue(await process_one_ocr_job(self.db))
        ready = await self.client.get(f"/api/v1/prescription-ocr-jobs/{job_id}")
        self.assertEqual(ready.status_code, 200, ready.text)
        self.assertEqual(ready.json()["status"], "ready_for_review")
        self.assertEqual(
            ready.json()["extracted_fields"]["items"][0]["medicine_name_snapshot"],
            "Amoxicillin",
        )
        self.assertNotIn("raw_text", ready.json()["extracted_fields"])

        drafts_url = f"{jobs_url}/{job_id}/draft"
        corrected_fields = {
            "claimed_prescriber": "Jane Example",
            "starts_on": "2026-11-28",
            "ends_on": "2026-12-02",
            "items": [
                {
                    "medicine_name_snapshot": "Amoxicillin",
                    "dose_text": "500 mg",
                    "route": "oral",
                    "frequency": "three times a day",
                    "duration_days": 5,
                    "instructions": "Take one capsule three times a day for five days.",
                    "quantity": "15",
                    "quantity_unit": "capsule",
                    "is_prn": False,
                }
            ],
        }
        draft = await self.client.post(
            drafts_url,
            json=corrected_fields,
            headers={"Idempotency-Key": "ml152-postgres-draft-1"},
        )
        self.assertEqual(draft.status_code, 201, draft.text)
        prescription = draft.json()["prescription"]["prescription"]
        prescription_id = prescription["id"]
        self.assertEqual(prescription["source"], "patient")
        self.assertEqual(prescription["status"], "draft")
        self.assertEqual(prescription["original_document_id"], str(self.document_id))
        self.assertIsNone(prescription["prescriber_user_id"])
        self.assertIsNone(prescription["issued_at"])

        draft_replay = await self.client.post(
            drafts_url,
            json=corrected_fields,
            headers={"Idempotency-Key": "ml152-postgres-draft-1"},
        )
        self.assertEqual(draft_replay.status_code, 201, draft_replay.text)
        self.assertEqual(draft_replay.json(), draft.json())
        draft_count = await self.db.scalar(
            select(Prescription.id).where(Prescription.id == prescription_id)
        )
        self.assertIsNotNone(draft_count)

        stored_job = await self.db.scalar(
            select(PrescriptionOcrJobModel).where(PrescriptionOcrJobModel.id == job_id)
        )
        self.assertEqual(stored_job.status, "draft_created")
        self.assertEqual(str(stored_job.resulting_prescription_id), prescription_id)
        self.assertEqual(
            Path(settings.private_upload_dir, self.storage_key).read_bytes(),
            self.original_bytes,
        )

        # Draft creation is not confirmation. The existing separate patient
        # action can confirm personal use without doctor issue or source promotion.
        confirm = await self.client.post(f"/api/v1/prescriptions/{prescription_id}/confirm")
        self.assertEqual(confirm.status_code, 200, confirm.text)
        confirmed = confirm.json()["prescription"]
        self.assertEqual(confirmed["source"], "patient")
        self.assertIsNone(confirmed["prescriber_user_id"])
        self.assertEqual(confirmed["status"], "active")
        self.assertIsNotNone(confirmed["issued_at"])

        foreign_patient = User(
            firebase_uid=f"ml152-foreign-{uuid4()}",
            display_name="Synthetic unrelated patient",
            status="active",
        )
        self.db.add(foreign_patient)
        await self.db.flush()
        self.db.add(
            PatientProfile(
                user_id=foreign_patient.id,
                date_of_birth=date(1988, 1, 1),
                sex="male",
            )
        )
        patient_role = await self.db.scalar(select(Role).where(Role.code == "patient"))
        self.db.add(UserRole(user_id=foreign_patient.id, role_id=patient_role.id))
        await self.db.commit()
        original_patient = self.patient
        self.patient = foreign_patient
        foreign_job = await self.client.get(f"/api/v1/prescription-ocr-jobs/{job_id}")
        self.assertEqual(foreign_job.status_code, 404)
        foreign_list = await self.client.get(jobs_url)
        self.assertEqual(foreign_list.status_code, 404)
        self.patient = original_patient

        events = list(
            (
                await self.db.scalars(
                    select(AuditEvent).where(
                        AuditEvent.target_type == "prescription_ocr_jobs",
                        AuditEvent.target_id == job_id,
                    )
                )
            ).all()
        )
        actions = {event.action for event in events}
        self.assertIn("prescription.ocr_job.queued", actions)
        self.assertIn("prescription.ocr_job.attempt.started", actions)
        self.assertIn("prescription.ocr_job.attempt.completed", actions)
        self.assertIn("prescription.ocr_job.draft_created", actions)

    async def test_failed_job_can_retry_original_and_stops_at_existing_attempt_bound(self):
        jobs_url = f"/api/v1/patients/{self.patient_id}/prescription-ocr-jobs"
        created = await self.client.post(
            jobs_url,
            json={"original_document_id": str(self.bad_document_id)},
            headers={"Idempotency-Key": "ml152-failure-create-1"},
        )
        self.assertEqual(created.status_code, 202, created.text)
        job_id = created.json()["id"]
        original = Path(settings.private_upload_dir, self.bad_storage_key).read_bytes()

        for expected_count in (1, 2, 3):
            self.assertTrue(await process_one_ocr_job(self.db))
            failed = await self.client.get(f"/api/v1/prescription-ocr-jobs/{job_id}")
            self.assertEqual(failed.json()["status"], "failed")
            self.assertEqual(failed.json()["attempt_count"], expected_count)
            self.assertEqual(failed.json()["error_code"], "OCR_UNREADABLE")
            self.assertEqual(
                Path(settings.private_upload_dir, self.bad_storage_key).read_bytes(), original
            )
            if expected_count < 3:
                retried = await self.client.post(
                    f"/api/v1/prescription-ocr-jobs/{job_id}/retry",
                    json={"reason": "Synthetic retry; no patient data"},
                )
                self.assertEqual(retried.status_code, 202, retried.text)
                self.assertEqual(retried.json()["status"], "queued")
            else:
                rejected = await self.client.post(
                    f"/api/v1/prescription-ocr-jobs/{job_id}/retry",
                    json={"reason": "Synthetic retry"},
                )
                self.assertEqual(rejected.status_code, 409)
                self.assertEqual(rejected.json()["code"], "RETRY_LIMIT_REACHED")

        events = list(
            (
                await self.db.scalars(
                    select(AuditEvent).where(
                        AuditEvent.target_type == "prescription_ocr_jobs",
                        AuditEvent.target_id == job_id,
                    )
                )
            ).all()
        )
        self.assertEqual(
            sum(event.action == "prescription.ocr_job.attempt.completed" for event in events),
            3,
        )
        self.assertTrue(
            all("patient data" not in str(event.metadata_) for event in events)
        )

    async def test_stale_worker_cannot_overwrite_a_newer_processing_attempt(self):
        jobs_url = f"/api/v1/patients/{self.patient_id}/prescription-ocr-jobs"
        created = await self.client.post(
            jobs_url,
            json={"original_document_id": str(self.document_id)},
            headers={"Idempotency-Key": "ml152-stale-attempt-create"},
        )
        self.assertEqual(created.status_code, 202, created.text)
        job_id = created.json()["id"]

        async def newer_attempt_claimed(*args, **kwargs):
            # The first worker has already committed attempt 1 as processing.
            # Model recovery/reclaim completing while its OCR call is still in
            # flight, leaving attempt 2 as the current processing owner.
            await self.db.execute(
                update(PrescriptionOcrJobModel)
                .where(PrescriptionOcrJobModel.id == job_id)
                .values(status="processing", attempt_count=2, extracted_fields=None)
            )
            await self.db.commit()
            return {
                "claimed_prescriber": None,
                "starts_on": None,
                "ends_on": None,
                "items": [],
            }

        with patch(
            "app.modules.prescriptions.ocr_worker.run_in_threadpool",
            side_effect=newer_attempt_claimed,
        ):
            self.assertTrue(await process_one_ocr_job(self.db))

        current = await self.db.scalar(
            select(PrescriptionOcrJobModel)
            .where(PrescriptionOcrJobModel.id == job_id)
            .execution_options(populate_existing=True)
        )
        self.assertEqual(current.status, "processing")
        self.assertEqual(current.attempt_count, 2)
        self.assertIsNone(current.extracted_fields)
        completions = await self.db.scalars(
            select(AuditEvent).where(
                AuditEvent.target_type == "prescription_ocr_jobs",
                AuditEvent.target_id == job_id,
                AuditEvent.action == "prescription.ocr_job.attempt.completed",
            )
        )
        self.assertEqual(list(completions.all()), [])

    async def test_concurrent_same_key_requests_replay_one_saved_response(self):
        actor_id = uuid4()
        target = f"synthetic-{uuid4()}"
        scope = IdempotencyScope(
            "user", actor_id, "createPrescriptionOcrJob", target, "ml152-concurrent-1"
        )
        digest = request_digest({"original_document_id": str(self.document_id)})
        response_body = {"id": str(uuid4()), "status": "queued"}
        start = Event()

        async def request_once(delay: float):
            await start.wait()
            async with AsyncSession(self.engine, expire_on_commit=False) as session:
                previous = await lock_and_replay(session, scope, digest, self._fetch_one)
                if previous is not None:
                    await session.commit()
                    return "replay", previous["response_body"]
                await sleep(delay)
                await save_success(session, scope, digest, 202, response_body)
                await session.commit()
                return "created", response_body

        first = create_task(request_once(0.15))
        second = create_task(request_once(0))
        start.set()
        results = await gather(first, second)
        self.assertEqual(sorted(result[0] for result in results), ["created", "replay"])
        self.assertEqual(results[0][1], results[1][1])

        async with AsyncSession(self.engine, expire_on_commit=False) as session:
            rows = await session.scalars(
                select(ApiIdempotencyRecord).where(
                    ApiIdempotencyRecord.actor_id == actor_id,
                    ApiIdempotencyRecord.operation_key == "createPrescriptionOcrJob",
                    ApiIdempotencyRecord.target_key == target,
                )
            )
            self.assertEqual(len(list(rows.all())), 1)
            await session.execute(
                delete(ApiIdempotencyRecord).where(
                    ApiIdempotencyRecord.actor_id == actor_id,
                    ApiIdempotencyRecord.operation_key == "createPrescriptionOcrJob",
                    ApiIdempotencyRecord.target_key == target,
                )
            )
            await session.commit()

    @staticmethod
    async def _fetch_one(session, statement, **params):
        return (await session.execute(text(statement), params)).mappings().first()
