"""ML-112 stale prescription edit integration tests."""

import asyncio
import os
from datetime import UTC, date, datetime, timedelta
from unittest import IsolatedAsyncioTestCase, skipUnless
from unittest.mock import patch
from uuid import uuid4

from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import models as _models  # noqa: F401
from app.database.session import get_session
from app.main import app
from app.modules.audit.models import AuditEvent
from app.modules.identity.auth import get_current_user
from app.modules.identity.models import Role, User, UserRole
from app.modules.identity.request_limits import require_authenticated_rate_limit
from app.modules.medication.enum import DoseResponse, DoseStatus, ScheduleStatus
from app.modules.medication.models import (
    DoseOccurrence,
    MedicationLog,
    MedicationSchedule,
    ScheduleTime,
)
from app.modules.prescriptions.core_models import Prescription, PrescriptionItem
from app.modules.prescriptions.review_models import PrescriptionReview, ReviewOutcome
from app.modules.profiles.models import PatientProfile

TEST_DATABASE_URL = os.getenv("ML112_TEST_DATABASE_URL")


@skipUnless(TEST_DATABASE_URL, "Set ML112_TEST_DATABASE_URL to a dedicated migrated test database")
class PrescriptionStaleEditTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine(TEST_DATABASE_URL)
        self.addAsyncCleanup(self.engine.dispose)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

        async with self.sessions() as session:
            role = await session.scalar(select(Role).where(Role.code == "patient"))
            if role is None:
                role = Role(code="patient", label="Patient")
                session.add(role)
                await session.flush()

            self.patient = User(
                id=uuid4(),
                firebase_uid=f"ml112-patient-{uuid4()}",
                display_name="ML-112 synthetic patient",
                status="active",
            )
            reviewer = User(
                id=uuid4(),
                firebase_uid=f"ml112-reviewer-{uuid4()}",
                display_name="ML-112 synthetic reviewer",
                status="active",
            )
            session.add_all([self.patient, reviewer])
            await session.flush()

            session.add_all([
                UserRole(user_id=self.patient.id, role_id=role.id),
                PatientProfile(
                    user_id=self.patient.id,
                    date_of_birth=date(1990, 1, 1),
                    sex="female",
                ),
            ])
            await session.flush()

            self.prescription = Prescription(
                patient_user_id=self.patient.id,
                source="patient",
                created_by=self.patient.id,
            )
            session.add(self.prescription)
            await session.flush()

            self.prescription_id = self.prescription.id
            self.original_item = PrescriptionItem(
                prescription_id=self.prescription.id,
                medicine_name_snapshot="Original medicine",
                dose_text="1 tablet",
                frequency="daily",
                is_prn=False,
            )
            session.add(self.original_item)

            session.add(
                PrescriptionReview(
                    prescription_id=self.prescription.id,
                    reviewer_user_id=reviewer.id,
                    outcome=ReviewOutcome.REVIEWED,
                    notes="Historical review evidence",
                )
            )
            await session.commit()

        async def session_override():
            async with self.sessions() as session:
                yield session

        async def actor_override():
            return self.patient

        self.enterContext(
            patch.dict(
                app.dependency_overrides,
                {
                    get_session: session_override,
                    get_current_user: actor_override,
                    require_authenticated_rate_limit: lambda: None,
                },
            )
        )

        self.client = AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        )
        self.addAsyncCleanup(self.client.aclose)

    def payload(self, medicine_name: str):
        return {
            "items": [
                {
                    "medicine_name_snapshot": medicine_name,
                    "dose_text": "1 tablet",
                    "frequency": "daily",
                    "is_prn": False,
                }
            ]
        }

    async def test_stale_version_cannot_overwrite_newer_edit(self):
        url = f"/api/v1/prescriptions/{self.prescription_id}/draft"

        first = await self.client.put(
            url,
            json=self.payload("Updated medicine"),
            headers={"If-Match": '"1"'},
        )
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json()["prescription"]["version"], 2)

        stale = await self.client.put(
            url,
            json=self.payload("Stale medicine"),
            headers={"If-Match": '"1"'},
        )
        self.assertEqual(stale.status_code, 409, stale.text)
        self.assertEqual(stale.json()["code"], "STALE_VERSION")

        async with self.sessions() as session:
            prescription = await session.get(Prescription, self.prescription_id)
            items = list(
                (
                    await session.scalars(
                        select(PrescriptionItem).where(
                            PrescriptionItem.prescription_id == self.prescription_id
                        )
                    )
                ).all()
            )
            reviews = list(
                (
                    await session.scalars(
                        select(PrescriptionReview).where(
                            PrescriptionReview.prescription_id == self.prescription_id
                        )
                    )
                ).all()
            )

            self.assertEqual(prescription.version, 2)
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0].medicine_name_snapshot, "Updated medicine")
            self.assertEqual(len(reviews), 1)
            self.assertEqual(reviews[0].notes, "Historical review evidence")

            event_count = await session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(
                    AuditEvent.target_id == self.prescription_id,
                    AuditEvent.action == "prescription.draft.updated",
                )
            )
            self.assertEqual(event_count, 1)

    async def test_concurrent_edits_with_same_version_allow_only_one(self):
        url = f"/api/v1/prescriptions/{self.prescription_id}/draft"

        responses = await asyncio.wait_for(
            asyncio.gather(
                self.client.put(
                    url,
                    json=self.payload("Concurrent medicine A"),
                    headers={"If-Match": '"1"'},
                ),
                self.client.put(
                    url,
                    json=self.payload("Concurrent medicine B"),
                    headers={"If-Match": '"1"'},
                ),
            ),
            timeout=20,
        )

        self.assertEqual(
            sorted(response.status_code for response in responses),
            [200, 409],
            [response.text for response in responses],
        )
        conflict = next(response for response in responses if response.status_code == 409)
        self.assertEqual(conflict.json()["code"], "STALE_VERSION")

        async with self.sessions() as session:
            prescription = await session.get(Prescription, self.prescription_id)
            items = list(
                (
                    await session.scalars(
                        select(PrescriptionItem).where(
                            PrescriptionItem.prescription_id == self.prescription_id
                        )
                    )
                ).all()
            )

            self.assertEqual(prescription.version, 2)
            self.assertEqual(len(items), 1)
            self.assertIn(
                items[0].medicine_name_snapshot,
                {"Concurrent medicine A", "Concurrent medicine B"},
            )

            event_count = await session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(
                    AuditEvent.target_id == self.prescription_id,
                    AuditEvent.action == "prescription.draft.updated",
                )
            )
            self.assertEqual(event_count, 1)





@skipUnless(TEST_DATABASE_URL, "Set ML112_TEST_DATABASE_URL to a dedicated migrated test database")
class ScheduleStaleEditTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine(TEST_DATABASE_URL)
        self.addAsyncCleanup(self.engine.dispose)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

        now = datetime.now(UTC)

        async with self.sessions() as session:
            role = await session.scalar(select(Role).where(Role.code == "patient"))
            if role is None:
                role = Role(code="patient", label="Patient")
                session.add(role)
                await session.flush()

            self.patient = User(
                id=uuid4(),
                firebase_uid=f"ml112-schedule-patient-{uuid4()}",
                display_name="ML-112 synthetic schedule patient",
                status="active",
            )
            session.add(self.patient)
            await session.flush()

            session.add_all([
                UserRole(user_id=self.patient.id, role_id=role.id),
                PatientProfile(
                    user_id=self.patient.id,
                    date_of_birth=date(1990, 1, 1),
                    sex="female",
                ),
            ])
            await session.flush()

            prescription = Prescription(
                patient_user_id=self.patient.id,
                source="patient",
                created_by=self.patient.id,
            )
            session.add(prescription)
            await session.flush()

            item = PrescriptionItem(
                prescription_id=prescription.id,
                medicine_name_snapshot="Schedule test medicine",
                dose_text="1 tablet",
                frequency="daily",
                is_prn=False,
            )
            session.add(item)
            await session.flush()

            self.schedule = MedicationSchedule(
                prescription_item_id=item.id,
                patient_user_id=self.patient.id,
                timezone="Asia/Colombo",
                starts_at=now - timedelta(days=7),
                confirmed_at=now - timedelta(days=7),
                status=ScheduleStatus.ACTIVE,
                version=1,
            )
            session.add(self.schedule)
            await session.flush()
            self.schedule_id = self.schedule.id

            session.add(
                ScheduleTime(
                    schedule_id=self.schedule.id,
                    time_of_day=datetime.strptime("08:00:00", "%H:%M:%S").time(),
                )
            )

            past_occurrence = DoseOccurrence(
                schedule_id=self.schedule.id,
                scheduled_for=now - timedelta(days=2),
                status=DoseStatus.SKIPPED,
            )
            future_occurrence = DoseOccurrence(
                schedule_id=self.schedule.id,
                scheduled_for=now + timedelta(days=2),
                status=DoseStatus.UPCOMING,
            )
            session.add_all([past_occurrence, future_occurrence])
            await session.flush()

            self.past_occurrence_id = past_occurrence.id
            self.future_occurrence_id = future_occurrence.id

            session.add(
                MedicationLog(
                    dose_occurrence_id=past_occurrence.id,
                    response=DoseResponse.SKIPPED,
                    recorded_by=self.patient.id,
                )
            )
            await session.commit()

        async def session_override():
            async with self.sessions() as session:
                yield session

        async def actor_override():
            return self.patient

        self.enterContext(
            patch.dict(
                app.dependency_overrides,
                {
                    get_session: session_override,
                    get_current_user: actor_override,
                    require_authenticated_rate_limit: lambda: None,
                },
            )
        )

        self.client = AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        )
        self.addAsyncCleanup(self.client.aclose)

    def payload(self):
        return {
            "prescription_item_id": str(
                self._prescription_item_id
            ),
            "timezone": "Asia/Colombo",
            "starts_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            "ends_at": None,
            "times": ["09:00:00"],
            "confirmed": True,
        }

    async def test_replacement_rejects_stale_version_and_preserves_history(self):
        async with self.sessions() as session:
            schedule = await session.get(MedicationSchedule, self.schedule_id)
            self._prescription_item_id = schedule.prescription_item_id

        url = f"/api/v1/medication-schedules/{self.schedule_id}/replacement"

        first = await self.client.post(
            url,
            json=self.payload(),
            headers={"If-Match": '"1"'},
        )
        self.assertEqual(first.status_code, 201, first.text)
        self.assertEqual(first.json()["schedule"]["version"], 1)
        new_schedule_id = first.json()["schedule"]["id"]

        stale = await self.client.post(
            url,
            json=self.payload(),
            headers={"If-Match": '"1"'},
        )
        self.assertEqual(stale.status_code, 409, stale.text)
        self.assertEqual(stale.json()["code"], "STALE_VERSION")

        async with self.sessions() as session:
            old_schedule = await session.get(
                MedicationSchedule, self.schedule_id
            )
            new_schedule = await session.get(
                MedicationSchedule, new_schedule_id
            )
            past_occurrence = await session.get(
                DoseOccurrence, self.past_occurrence_id
            )
            future_occurrence = await session.get(
                DoseOccurrence, self.future_occurrence_id
            )

            logs = list(
                (
                    await session.scalars(
                        select(MedicationLog).where(
                            MedicationLog.dose_occurrence_id
                            == self.past_occurrence_id
                        )
                    )
                ).all()
            )

            schedules_count = await session.scalar(
                select(func.count())
                .select_from(MedicationSchedule)
                .where(
                    MedicationSchedule.prescription_item_id
                    == old_schedule.prescription_item_id
                )
            )

            self.assertEqual(old_schedule.status, ScheduleStatus.CANCELLED)
            self.assertEqual(old_schedule.version, 2)
            self.assertEqual(new_schedule.status, ScheduleStatus.ACTIVE)
            self.assertEqual(past_occurrence.status, DoseStatus.SKIPPED)
            self.assertEqual(future_occurrence.status, DoseStatus.CANCELLED)
            self.assertEqual(len(logs), 1)
            self.assertEqual(logs[0].response, DoseResponse.SKIPPED)
            self.assertEqual(schedules_count, 2)
