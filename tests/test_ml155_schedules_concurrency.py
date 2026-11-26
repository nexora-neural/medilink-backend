"""ML-155 independent-session races on a disposable PostgreSQL database."""

import asyncio
import os
from datetime import UTC, datetime, timedelta
from unittest import IsolatedAsyncioTestCase, skipUnless
from unittest.mock import patch
from uuid import UUID

from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.database.session import get_session
from app.main import app
from app.modules.identity.auth import get_current_user
from app.modules.identity.request_limits import require_authenticated_rate_limit
from app.modules.medication.models import MedicationSchedule, ScheduleStatusChange
from app.modules.prescriptions.core_models import Prescription, PrescriptionItem
from tests import test_ml155_schedules_postgres as fixtures

URL = os.getenv("ML155_CONCURRENCY_DATABASE_URL")


@skipUnless(URL, "Set ML155_CONCURRENCY_DATABASE_URL to a separate migrated disposable DB")
class ScheduleConcurrencyTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine(URL)
        self.addAsyncCleanup(self.engine.dispose)
        self.db = AsyncSession(self.engine, expire_on_commit=False)
        self.addAsyncCleanup(self.db.close)
        self.patient = await fixtures.SchedulePostgresTests.user(self, "patient")
        now = datetime.now(UTC)
        prescription = Prescription(
            patient_user_id=self.patient.id,
            created_by=self.patient.id,
            source="patient",
            status="active",
            issued_at=now - timedelta(days=1),
        )
        self.db.add(prescription)
        await self.db.flush()
        item = PrescriptionItem(
            prescription_id=prescription.id,
            medicine_name_snapshot="Synthetic concurrent medicine",
            dose_text="Original dose",
            frequency="daily",
            is_prn=False,
        )
        self.db.add(item)
        await self.db.commit()
        self.body = {
            "prescription_item_id": str(item.id),
            "timezone": "Asia/Colombo",
            "starts_at": (now + timedelta(hours=1)).isoformat(),
            "ends_at": (now + timedelta(days=5)).isoformat(),
            "times": ["08:00:00"],
            "confirmed": True,
        }
        self.path = f"/api/v1/patients/{self.patient.id}/medication-schedules"

        async def session():
            async with AsyncSession(self.engine, expire_on_commit=False) as db:
                yield db

        async def actor():
            return self.patient

        override = patch.dict(
            app.dependency_overrides,
            {
                get_session: session,
                get_current_user: actor,
                require_authenticated_rate_limit: lambda: None,
            },
        )
        override.start()
        self.addCleanup(override.stop)
        self.client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        self.addAsyncCleanup(self.client.aclose)

    async def concurrent(self, *requests):
        return await asyncio.wait_for(asyncio.gather(*requests), 10)

    async def test_concurrent_confirmation_allows_only_one_overlapping_schedule(self):
        responses = await self.concurrent(
            self.client.post(self.path, json=self.body), self.client.post(self.path, json=self.body)
        )
        self.assertEqual(
            sorted(r.status_code for r in responses), [201, 409], [r.text for r in responses]
        )
        count = await self.db.scalar(
            select(func.count())
            .select_from(MedicationSchedule)
            .where(MedicationSchedule.patient_user_id == self.patient.id)
        )
        self.assertEqual(count, 1)

    async def test_concurrent_status_changes_with_same_version_cannot_overwrite(self):
        schedule = (await self.client.post(self.path, json=self.body)).json()["schedule"]["id"]
        path = f"/api/v1/medication-schedules/{schedule}/status"
        responses = await self.concurrent(
            self.client.post(
                path,
                json={"status": "paused", "reason": "Synthetic pause"},
                headers={"If-Match": "1"},
            ),
            self.client.post(
                path,
                json={"status": "cancelled", "reason": "Synthetic end"},
                headers={"If-Match": "1"},
            ),
        )
        self.assertEqual(
            sorted(r.status_code for r in responses), [201, 409], [r.text for r in responses]
        )
        count = await self.db.scalar(
            select(func.count())
            .select_from(ScheduleStatusChange)
            .where(ScheduleStatusChange.schedule_id == UUID(schedule))
        )
        self.assertEqual(count, 1)

    async def test_concurrent_replacements_keep_one_successor(self):
        old = (await self.client.post(self.path, json=self.body)).json()["schedule"]["id"]
        path = f"/api/v1/medication-schedules/{old}/replacement"
        responses = await self.concurrent(
            self.client.post(
                path, json=self.body | {"times": ["09:00:00"]}, headers={"If-Match": "1"}
            ),
            self.client.post(
                path, json=self.body | {"times": ["10:00:00"]}, headers={"If-Match": "1"}
            ),
        )
        self.assertEqual(
            sorted(r.status_code for r in responses), [201, 409], [r.text for r in responses]
        )
        rows = (
            await self.db.scalars(
                select(MedicationSchedule).where(
                    MedicationSchedule.patient_user_id == self.patient.id
                )
            )
        ).all()
        self.assertEqual(len(rows), 2)
        successor = next(row for row in rows if str(row.id) != old)
        self.assertEqual(str(successor.previous_schedule_id), old)
        self.assertEqual(successor.status, "active")
