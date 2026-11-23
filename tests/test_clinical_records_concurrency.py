"""Real committed-session test of write versus grant revocation ordering."""

import asyncio
import os
from datetime import UTC, datetime
from unittest import IsolatedAsyncioTestCase, skipUnless
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.modules.audit.service import append_event
from app.modules.clinical_records import service
from app.modules.clinical_records.models import ConditionsModel
from app.modules.clinical_records.schemas import ClinicalInput
from app.modules.consent.models import AccessGrant
from tests import test_clinical_records_postgres as fixtures

URL = os.getenv("ML68_CONCURRENCY_DATABASE_URL")


@skipUnless(URL, "Set ML68_CONCURRENCY_DATABASE_URL to a separate migrated disposable DB")
class ClinicalConcurrencyTests(IsolatedAsyncioTestCase):
    async def test_authorized_write_commits_before_waiting_revocation_and_next_write_denied(self):
        engine = create_async_engine(URL)
        self.addAsyncCleanup(engine.dispose)
        self.db = AsyncSession(engine, expire_on_commit=False)
        self.addAsyncCleanup(self.db.close)
        self.patient = await fixtures.ClinicalPostgresTests.user(self, "patient")
        self.doctor = await fixtures.ClinicalPostgresTests.user(self, "doctor")
        grant, consultation = await fixtures.ClinicalPostgresTests.context(self)
        patient_id, grant_id = self.patient.id, grant.id
        body = ClinicalInput(
            type="condition",
            title="Synthetic race test",
            recorded_at=datetime.now(UTC),
            consultation_id=consultation.id,
        )
        ready, release = asyncio.Event(), asyncio.Event()

        async def held_audit(*args, **kwargs):
            ready.set()
            await asyncio.wait_for(release.wait(), 5)
            return await append_event(*args, **kwargs)

        async def write():
            async with AsyncSession(engine, expire_on_commit=False) as db:
                return await service.create_record(db, self.doctor, patient_id, body, "ml68-race")

        async def revoke():
            async with engine.begin() as connection:
                await connection.execute(
                    update(AccessGrant)
                    .where(AccessGrant.id == grant_id)
                    .values(
                        status="revoked",
                        revoked_at=datetime.now(UTC),
                    )
                )

        writer = revoker = None
        try:
            with patch.object(service, "append_event", side_effect=held_audit):
                writer = asyncio.create_task(write())
                await asyncio.wait_for(ready.wait(), 5)
                revoker = asyncio.create_task(revoke())
                with self.assertRaises(TimeoutError):
                    await asyncio.wait_for(asyncio.shield(revoker), 0.15)
                release.set()
                record = await asyncio.wait_for(writer, 5)
                await asyncio.wait_for(revoker, 5)
            self.assertEqual(record.source, "doctor")
            async with AsyncSession(engine, expire_on_commit=False) as db:
                with self.assertRaises(HTTPException) as denied:
                    await service.create_record(db, self.doctor, patient_id, body, "ml68-after")
                self.assertEqual(denied.exception.status_code, 404)
                count = await db.scalar(
                    select(func.count())
                    .select_from(ConditionsModel)
                    .where(
                        ConditionsModel.patient_user_id == patient_id,
                    )
                )
                self.assertEqual(count, 1)
        finally:
            release.set()
            for task in (writer, revoker):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(
                *(t for t in (writer, revoker) if t is not None), return_exceptions=True
            )
