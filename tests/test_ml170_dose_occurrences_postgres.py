"""ML-170 API/worker integration tests against an isolated PostgreSQL database."""

import asyncio
import os
from datetime import UTC, date, datetime, time, timedelta
from unittest import IsolatedAsyncioTestCase, skipUnless
from unittest.mock import patch
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database.session import get_session
from app.main import app
from app.modules.audit.models import AuditEvent
from app.modules.consent.models import AccessGrant, GrantCategory
from app.modules.identity.auth import get_current_user
from app.modules.identity.models import UserRole
from app.modules.identity.request_limits import require_authenticated_rate_limit
from app.modules.medication import service
from app.modules.medication.enum import DoseStatus
from app.modules.medication.models import (
    DoseOccurrence,
    MedicationLog,
    MedicationSchedule,
    ScheduleStatusChange,
    ScheduleTime,
)
from app.modules.medication.occurrence_worker import _local_instant, process_active_schedules
from app.modules.notifications.models import Notification
from app.modules.prescriptions.core_models import Prescription, PrescriptionItem
from app.modules.profiles.models import PatientProfile, ProfessionalProfile
from tests import test_ml155_schedules_postgres as ml155

URL = os.getenv("ML170_TEST_DATABASE_URL")
CONCURRENCY_URL = os.getenv("ML170_CONCURRENCY_DATABASE_URL")


@skipUnless(URL, "Set ML170_TEST_DATABASE_URL to a dedicated migrated PostgreSQL database")
class DoseOccurrencePostgresTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine(URL)
        self.addAsyncCleanup(self.engine.dispose)
        connection = await self.engine.connect()
        self.addAsyncCleanup(connection.close)
        transaction = await connection.begin()
        self.addAsyncCleanup(transaction.rollback)
        self.db = AsyncSession(
            bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )
        self.addAsyncCleanup(self.db.close)

        self.patient = await ml155.SchedulePostgresTests.user(self, "patient")
        self.other = await ml155.SchedulePostgresTests.user(self, "patient")
        self.doctor = await ml155.SchedulePostgresTests.user(self, "doctor")
        self.now = datetime.now(UTC).replace(microsecond=0)
        scheduled = self.now - timedelta(minutes=5)
        self.daily_time = scheduled.time().replace(microsecond=0)
        self.scheduled_today = datetime.combine(scheduled.date(), self.daily_time, UTC)
        self.prescription = Prescription(
            patient_user_id=self.patient.id,
            source="patient",
            created_by=self.patient.id,
            status="active",
            issued_at=self.now - timedelta(days=2),
            starts_on=self.now.date() - timedelta(days=2),
            ends_on=self.now.date() + timedelta(days=10),
        )
        self.db.add(self.prescription)
        await self.db.flush()
        self.item = PrescriptionItem(
            prescription_id=self.prescription.id,
            medicine_name_snapshot="Synthetic ML-170 medicine",
            dose_text="Synthetic dose",
            frequency="daily",
            instructions="Synthetic test only",
            duration_days=13,
            is_prn=False,
        )
        self.db.add(self.item)
        await self.db.commit()
        self.actor = self.patient

        async def session():
            try:
                yield self.db
            finally:
                for row in list(self.db.identity_map.values()):
                    await self.db.refresh(row)

        async def actor():
            return self.actor

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
        self.list_path = f"/api/v1/patients/{self.patient.id}/dose-occurrences"
        self.schedule_path = f"/api/v1/patients/{self.patient.id}/medication-schedules"
        self.body = {
            "prescription_item_id": str(self.item.id),
            "timezone": "UTC",
            "starts_at": (self.now - timedelta(days=2)).isoformat(),
            "ends_at": (self.now + timedelta(days=5)).isoformat(),
            "times": [self.daily_time.isoformat()],
            "confirmed": True,
        }

    async def create_schedule(self):
        response = await self.client.post(self.schedule_path, json=self.body)
        self.assertEqual(response.status_code, 201, response.text)
        return UUID(response.json()["schedule"]["id"])

    async def add_occurrence(
        self,
        schedule_id,
        scheduled_for,
        *,
        status="unreported",
        snoozed_until=None,
        response=None,
    ):
        occurrence = DoseOccurrence(
            schedule_id=schedule_id,
            scheduled_for=scheduled_for,
            status=status,
            snoozed_until=snoozed_until,
        )
        self.db.add(occurrence)
        await self.db.flush()
        if response is not None:
            self.db.add(
                MedicationLog(
                    dose_occurrence_id=occurrence.id,
                    response=response,
                    recorded_by=self.patient.id,
                    reported_taken_at=self.now if response == "taken" else None,
                )
            )
        await self.db.commit()
        return occurrence

    async def snooze(self, occurrence_id, until):
        return await self.client.post(
            f"/api/v1/dose-occurrences/{occurrence_id}/snooze",
            json={"snoozed_until": until.isoformat()},
        )

    async def grant(self, *, category="medication_logs", expires_at=None):
        grant = AccessGrant(
            patient_user_id=self.patient.id,
            professional_user_id=self.doctor.id,
            organization_id=None,
            status="active",
            allow_consultation=False,
            starts_at=self.now - timedelta(hours=1),
            expires_at=expires_at or self.now + timedelta(hours=1),
        )
        self.db.add(grant)
        await self.db.flush()
        self.db.add(GrantCategory(grant_id=grant.id, category=category))
        await self.db.commit()
        return grant

    async def test_end_to_end_generation_unreported_snooze_and_retry_invariants(self):
        schedule_id = await self.create_schedule()
        schedule = await self.db.get(MedicationSchedule, schedule_id)
        schedule.confirmed_at = self.scheduled_today - timedelta(days=1)
        schedule.occurrences_generated_through = self.scheduled_today - timedelta(seconds=1)
        await self.db.commit()

        first_changes = await process_active_schedules(self.db, now=self.now)
        await self.db.commit()
        self.assertGreater(first_changes, 0)
        occurrence = await self.db.scalar(
            select(DoseOccurrence).where(
                DoseOccurrence.schedule_id == schedule_id,
                DoseOccurrence.scheduled_for == self.scheduled_today,
            )
        )
        self.assertIsNotNone(occurrence)
        self.assertEqual(occurrence.status, DoseStatus.UNREPORTED)
        original_time = occurrence.scheduled_for
        original_status = occurrence.status
        occurrence_count = await self.db.scalar(select(func.count()).select_from(DoseOccurrence))
        initial_notifications = await self.db.scalar(
            select(func.count())
            .select_from(Notification)
            .where(Notification.related_id == occurrence.id)
        )
        self.assertEqual(initial_notifications, 1)

        await process_active_schedules(self.db, now=self.now)
        await self.db.commit()
        self.assertEqual(
            await self.db.scalar(select(func.count()).select_from(DoseOccurrence)), occurrence_count
        )
        self.assertEqual(
            await self.db.scalar(
                select(func.count())
                .select_from(Notification)
                .where(Notification.related_id == occurrence.id)
            ),
            1,
        )

        listing = await self.client.get(
            self.list_path,
            params={
                "from": (self.scheduled_today - timedelta(minutes=1)).isoformat(),
                "to": (self.scheduled_today + timedelta(minutes=1)).isoformat(),
                "status": "unreported",
                "page": 1,
                "page_size": 5,
            },
        )
        self.assertEqual(listing.status_code, 200, listing.text)
        self.assertEqual(listing.json()["total"], 1)
        self.assertEqual(listing.json()["items"][0]["id"], str(occurrence.id))

        denominator_before = await self.db.scalar(
            select(func.count())
            .select_from(DoseOccurrence)
            .where(
                DoseOccurrence.scheduled_for <= self.now,
                DoseOccurrence.status.in_(
                    [DoseStatus.TAKEN, DoseStatus.SKIPPED, DoseStatus.UNREPORTED]
                ),
            )
        )
        snooze_until = self.now + timedelta(minutes=20)
        response = await self.snooze(occurrence.id, snooze_until)
        self.assertEqual(response.status_code, 201, response.text)
        returned_time = datetime.fromisoformat(
            response.json()["scheduled_for"].replace("Z", "+00:00")
        )
        self.assertEqual(returned_time, original_time)
        self.assertEqual(response.json()["status"], original_status)
        returned_snooze = datetime.fromisoformat(
            response.json()["snoozed_until"].replace("Z", "+00:00")
        )
        self.assertEqual(returned_snooze, snooze_until)
        await self.db.refresh(occurrence)
        denominator_after = await self.db.scalar(
            select(func.count())
            .select_from(DoseOccurrence)
            .where(
                DoseOccurrence.scheduled_for <= self.now,
                DoseOccurrence.status.in_(
                    [DoseStatus.TAKEN, DoseStatus.SKIPPED, DoseStatus.UNREPORTED]
                ),
            )
        )
        self.assertEqual(denominator_after, denominator_before)
        self.assertEqual(occurrence.scheduled_for, original_time)
        self.assertEqual(occurrence.status, original_status)

        await process_active_schedules(self.db, now=snooze_until + timedelta(seconds=1))
        await self.db.commit()
        await self.db.refresh(occurrence)
        self.assertIsNone(occurrence.snoozed_until)
        self.assertEqual(occurrence.status, original_status)
        reminder_count = await self.db.scalar(
            select(func.count())
            .select_from(Notification)
            .where(Notification.related_id == occurrence.id)
        )
        self.assertEqual(reminder_count, 2)
        self.assertIsNotNone(
            await self.db.scalar(
                select(AuditEvent.id).where(
                    AuditEvent.action == "dose_occurrence.snoozed",
                    AuditEvent.target_id == occurrence.id,
                )
            )
        )

    async def test_batch_rotation_processes_every_schedule(self):
        schedule_id = await self.create_schedule()
        first = await self.db.get(MedicationSchedule, schedule_id)
        first.confirmed_at = self.scheduled_today - timedelta(days=1)
        first.occurrences_generated_through = self.scheduled_today - timedelta(seconds=1)
        item = PrescriptionItem(
            prescription_id=self.prescription.id,
            medicine_name_snapshot="Synthetic second medicine",
            dose_text="Synthetic dose", frequency="daily", duration_days=13, is_prn=False,
        )
        self.db.add(item)
        await self.db.flush()
        second = MedicationSchedule(
            id=uuid4(), prescription_item_id=item.id, patient_user_id=self.patient.id,
            timezone="UTC", starts_at=first.starts_at, ends_at=first.ends_at,
            confirmed_at=first.confirmed_at,
            occurrences_generated_through=first.occurrences_generated_through,
            status="active", version=1,
        )
        self.db.add(second)
        await self.db.flush()
        self.db.add(ScheduleTime(schedule_id=second.id, time_of_day=self.daily_time))
        await self.db.commit()
        for _ in range(4):
            await process_active_schedules(self.db, now=self.now, batch_size=1)
            await self.db.commit()
        for schedule in (first, second):
            count = await self.db.scalar(
                select(func.count()).select_from(DoseOccurrence).where(
                    DoseOccurrence.schedule_id == schedule.id
                )
            )
            self.assertGreater(count, 0)

    async def test_snooze_before_first_reminder_replaces_original(self):
        schedule_id = await self.create_schedule()
        schedule = await self.db.get(MedicationSchedule, schedule_id)
        schedule.confirmed_at = self.now - timedelta(days=1)
        occurrence = await self.add_occurrence(
            schedule_id, self.now - timedelta(minutes=1), status="unreported"
        )
        until = self.now + timedelta(minutes=10)
        response = await self.snooze(occurrence.id, until)
        self.assertEqual(response.status_code, 201, response.text)
        for instant in (self.now, until + timedelta(seconds=1), until + timedelta(seconds=2)):
            await process_active_schedules(self.db, now=instant)
            await self.db.commit()
        self.assertEqual(
            await self.db.scalar(select(func.count()).select_from(Notification).where(
                Notification.related_id == occurrence.id
            )), 1
        )
        await self.db.refresh(occurrence)
        self.assertIsNone(occurrence.snoozed_until)

    async def test_worker_sets_due_at_schedule_then_unreported_on_first_later_evaluation(self):
        schedule_id = await self.create_schedule()
        schedule = await self.db.get(MedicationSchedule, schedule_id)
        schedule.confirmed_at = self.now - timedelta(days=1)
        schedule.occurrences_generated_through = self.now - timedelta(seconds=1)
        scheduled_for = self.now
        schedule_time = await self.db.scalar(
            select(ScheduleTime).where(ScheduleTime.schedule_id == schedule_id)
        )
        schedule_time.time_of_day = scheduled_for.time()
        await self.db.commit()

        await process_active_schedules(self.db, now=scheduled_for)
        await self.db.commit()
        occurrence = await self.db.scalar(
            select(DoseOccurrence).where(
                DoseOccurrence.schedule_id == schedule_id,
                DoseOccurrence.scheduled_for == scheduled_for,
            )
        )
        self.assertEqual(occurrence.status, DoseStatus.DUE)

        await process_active_schedules(self.db, now=scheduled_for + timedelta(microseconds=1))
        await self.db.commit()
        await self.db.refresh(occurrence)
        self.assertEqual(occurrence.status, DoseStatus.UNREPORTED)
        self.assertEqual(occurrence.scheduled_for, scheduled_for)

    async def test_list_filters_sorting_pagination_and_patient_ownership(self):
        schedule_id = await self.create_schedule()
        past = await self.add_occurrence(
            schedule_id, self.now - timedelta(hours=3), status="unreported"
        )
        middle = await self.add_occurrence(
            schedule_id,
            self.now - timedelta(hours=2),
            status="skipped",
            response="skipped",
        )
        await self.add_occurrence(schedule_id, self.now + timedelta(hours=1), status="upcoming")

        response = await self.client.get(
            self.list_path,
            params={
                "from": (self.now - timedelta(hours=4)).isoformat(),
                "to": self.now.isoformat(),
                "page": 2,
                "page_size": 1,
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["total"], 2)
        self.assertEqual(response.json()["items"][0]["id"], str(middle.id))
        filtered = await self.client.get(self.list_path, params={"status": "unreported"})
        self.assertEqual(filtered.json()["total"], 1)
        self.assertEqual(filtered.json()["items"][0]["id"], str(past.id))

        self.actor = self.other
        denied = await self.client.get(self.list_path)
        self.assertEqual(denied.status_code, 404)
        self.assertNotIn(str(self.patient.id), denied.text)

    async def test_doctor_read_requires_verified_current_medication_grant(self):
        await self.create_schedule()
        self.actor = self.doctor
        self.assertEqual((await self.client.get(self.list_path)).status_code, 404)
        wrong = await self.grant(category="prescriptions")
        self.assertEqual((await self.client.get(self.list_path)).status_code, 404)
        await self.db.delete(await self.db.get(GrantCategory, (wrong.id, "prescriptions")))
        wrong.status = "revoked"
        wrong.revoked_at = self.now
        await self.db.commit()
        expired = await self.grant(expires_at=self.now - timedelta(minutes=1))
        self.assertEqual((await self.client.get(self.list_path)).status_code, 404)
        expired.status = "revoked"
        expired.revoked_at = self.now
        await self.db.commit()
        valid = await self.grant()
        self.assertEqual((await self.client.get(self.list_path)).status_code, 200)
        valid.revoked_at = self.now
        valid.status = "revoked"
        await self.db.commit()
        self.assertEqual((await self.client.get(self.list_path)).status_code, 404)

        profile = await self.db.get(ProfessionalProfile, self.doctor.id)
        profile.verification_status = "not_submitted"
        await self.db.commit()
        valid.status = "active"
        valid.revoked_at = None
        valid.expires_at = self.now + timedelta(hours=1)
        await self.db.commit()
        self.assertEqual((await self.client.get(self.list_path)).status_code, 403)

    async def test_snooze_bounds_ineligible_states_and_patient_only_write(self):
        schedule_id = await self.create_schedule()
        eligible = await self.add_occurrence(schedule_id, self.now - timedelta(minutes=1))
        self.actor = self.other
        self.assertEqual(
            (await self.snooze(eligible.id, self.now + timedelta(minutes=10))).status_code, 404
        )
        self.actor = self.doctor
        await self.grant()
        self.assertEqual(
            (await self.snooze(eligible.id, self.now + timedelta(minutes=10))).status_code, 403
        )
        self.actor = self.patient

        for invalid in (
            self.now - timedelta(seconds=1),
            self.now + timedelta(hours=25),
        ):
            response = await self.snooze(eligible.id, invalid)
            self.assertEqual(response.status_code, 422, response.text)
        exact_limit = await self.db.scalar(select(func.clock_timestamp()))
        accepted = await self.snooze(eligible.id, exact_limit + timedelta(hours=24))
        self.assertEqual(accepted.status_code, 201, accepted.text)

        due = await self.add_occurrence(
            schedule_id, self.now - timedelta(minutes=2), status="due"
        )
        due_snooze = await self.snooze(due.id, self.now + timedelta(minutes=10))
        self.assertEqual(due_snooze.status_code, 201, due_snooze.text)

        future = await self.add_occurrence(
            schedule_id, self.now + timedelta(hours=1), status="due"
        )
        self.assertEqual(
            (await self.snooze(future.id, self.now + timedelta(hours=2))).status_code, 409
        )
        cancelled = await self.add_occurrence(
            schedule_id, self.now - timedelta(hours=1), status="cancelled"
        )
        self.assertEqual(
            (await self.snooze(cancelled.id, self.now + timedelta(hours=1))).status_code, 409
        )
        answered = await self.add_occurrence(
            schedule_id,
            self.now - timedelta(hours=1, minutes=1),
            status="taken",
            response="taken",
        )
        self.assertEqual(
            (await self.snooze(answered.id, self.now + timedelta(hours=1))).status_code, 409
        )

        schedule = await self.db.get(MedicationSchedule, schedule_id)
        schedule.ends_at = self.now + timedelta(minutes=15)
        await self.db.commit()
        out_of_window = await self.snooze(eligible.id, self.now + timedelta(minutes=20))
        self.assertEqual(out_of_window.status_code, 422, out_of_window.text)

        schedule.ends_at = self.now - timedelta(seconds=1)
        await self.db.commit()
        expired = await self.snooze(eligible.id, self.now + timedelta(minutes=5))
        self.assertEqual(expired.status_code, 409, expired.text)

    async def test_schedule_stop_cancels_future_unanswered_and_clears_snooze(self):
        schedule_id = await self.create_schedule()
        schedule = await self.db.get(MedicationSchedule, schedule_id)
        schedule.starts_at = self.now - timedelta(days=2)
        await self.db.flush()
        past = await self.add_occurrence(
            schedule_id,
            self.now - timedelta(hours=1),
            status="upcoming",
            snoozed_until=self.now + timedelta(minutes=30),
        )
        future = await self.add_occurrence(
            schedule_id,
            self.now + timedelta(hours=2),
            status="upcoming",
            snoozed_until=self.now + timedelta(hours=3),
        )
        answered = await self.add_occurrence(
            schedule_id,
            self.now + timedelta(hours=4),
            status="taken",
            snoozed_until=self.now + timedelta(hours=5),
            response="taken",
        )

        response = await self.client.post(
            f"/api/v1/medication-schedules/{schedule_id}/status",
            json={"status": "completed", "reason": "Synthetic completion"},
            headers={"If-Match": "1"},
        )
        self.assertEqual(response.status_code, 201, response.text)
        await self.db.refresh(past)
        await self.db.refresh(future)
        await self.db.refresh(answered)
        self.assertEqual(past.status, DoseStatus.UNREPORTED)
        self.assertIsNone(past.snoozed_until)
        self.assertEqual(future.status, DoseStatus.CANCELLED)
        self.assertIsNone(future.snoozed_until)
        self.assertEqual(answered.status, DoseStatus.TAKEN)
        self.assertEqual(answered.snoozed_until, self.now + timedelta(hours=5))
        self.assertEqual(
            await self.db.scalar(
                select(func.count())
                .select_from(ScheduleStatusChange)
                .where(ScheduleStatusChange.schedule_id == schedule_id)
            ),
            1,
        )

    async def test_snooze_audit_failure_rolls_back_pending_time(self):
        schedule_id = await self.create_schedule()
        occurrence = await self.add_occurrence(schedule_id, self.now - timedelta(minutes=1))
        until = self.now + timedelta(minutes=10)
        with patch.object(service, "append_event", side_effect=RuntimeError("private audit data")):
            response = await self.snooze(occurrence.id, until)
        self.assertEqual(response.status_code, 500)
        self.assertNotIn("private audit data", response.text)
        await self.db.refresh(occurrence)
        self.assertIsNone(occurrence.snoozed_until)

    async def test_dst_wall_clock_policy(self):
        # New York skips 02:30 on spring-forward and repeats 01:30 in autumn.
        spring_time = _local_instant(
            date(2026, 3, 8), time(2, 30), ZoneInfo("America/New_York")
        )
        self.assertIsNone(spring_time)
        earlier = _local_instant(date(2026, 11, 1), time(1, 30), ZoneInfo("America/New_York"))
        self.assertEqual(earlier, datetime(2026, 11, 1, 5, 30, tzinfo=UTC))


@skipUnless(
    CONCURRENCY_URL,
    "Set ML170_CONCURRENCY_DATABASE_URL to a separate migrated disposable database",
)
class DoseOccurrenceConcurrencyTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine(CONCURRENCY_URL)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.addAsyncCleanup(self.engine.dispose)
        self.patient = None
        self.schedule_id = None
        self.occurrence_id = None

        async with self.sessions() as session:
            self.db = session
            self.patient = await ml155.SchedulePostgresTests.user(self, "patient")
            now = await session.scalar(select(func.clock_timestamp()))
            prescription = Prescription(
                patient_user_id=self.patient.id,
                source="patient",
                created_by=self.patient.id,
                status="active",
                issued_at=now - timedelta(days=2),
                starts_on=now.date() - timedelta(days=2),
                ends_on=now.date() + timedelta(days=10),
            )
            session.add(prescription)
            await session.flush()
            item = PrescriptionItem(
                prescription_id=prescription.id,
                medicine_name_snapshot="Synthetic ML-170 concurrency medicine",
                dose_text="Synthetic dose",
                frequency="daily",
                duration_days=13,
                is_prn=False,
            )
            session.add(item)
            await session.flush()
            self.schedule_id = uuid4()
            schedule = MedicationSchedule(
                id=self.schedule_id,
                prescription_item_id=item.id,
                patient_user_id=self.patient.id,
                timezone="UTC",
                starts_at=now - timedelta(days=1),
                ends_at=now + timedelta(hours=1),
                confirmed_at=now - timedelta(days=1),
                occurrences_generated_through=now - timedelta(days=1),
                status="active",
                version=1,
            )
            session.add(schedule)
            self.scheduled_for = (now - timedelta(minutes=5)).replace(second=0, microsecond=0)
            schedule.occurrences_generated_through = self.scheduled_for - timedelta(seconds=1)
            session.add(
                ScheduleTime(
                    schedule_id=self.schedule_id, time_of_day=self.scheduled_for.time()
                )
            )
            await session.commit()

        async def session_override():
            async with self.sessions() as session:
                yield session

        async def actor_override():
            return self.patient

        self.override = patch.dict(
            app.dependency_overrides,
            {
                get_session: session_override,
                get_current_user: actor_override,
                require_authenticated_rate_limit: lambda: None,
            },
        )
        self.override.start()
        self.addCleanup(self.override.stop)
        self.client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        self.addAsyncCleanup(self.client.aclose)
        self.addAsyncCleanup(self.cleanup_synthetic_rows)

    async def cleanup_synthetic_rows(self):
        if self.patient is None:
            return
        async with self.sessions() as session:
            occurrence_ids = select(DoseOccurrence.id).where(
                DoseOccurrence.schedule_id == self.schedule_id
            )
            await session.execute(
                delete(Notification).where(Notification.related_id.in_(occurrence_ids))
            )
            # Audit rows are append-only. Keep their synthetic actor row in this
            # disposable database; the entire test database is dropped after use.
            await session.execute(
                delete(MedicationLog).where(MedicationLog.dose_occurrence_id.in_(occurrence_ids))
            )
            await session.execute(
                delete(DoseOccurrence).where(DoseOccurrence.schedule_id == self.schedule_id)
            )
            await session.execute(
                delete(ScheduleTime).where(ScheduleTime.schedule_id == self.schedule_id)
            )
            await session.execute(
                delete(ScheduleStatusChange).where(
                    ScheduleStatusChange.schedule_id == self.schedule_id
                )
            )
            await session.execute(
                delete(MedicationSchedule).where(MedicationSchedule.id == self.schedule_id)
            )
            item_ids = select(PrescriptionItem.id).where(
                PrescriptionItem.prescription_id.in_(
                    select(Prescription.id).where(Prescription.patient_user_id == self.patient.id)
                )
            )
            await session.execute(delete(PrescriptionItem).where(PrescriptionItem.id.in_(item_ids)))
            await session.execute(
                delete(Prescription).where(Prescription.patient_user_id == self.patient.id)
            )
            await session.execute(
                delete(PatientProfile).where(PatientProfile.user_id == self.patient.id)
            )
            await session.execute(delete(UserRole).where(UserRole.user_id == self.patient.id))
            await session.commit()

    async def test_concurrent_worker_retry_and_snooze_keep_one_occurrence(self):
        now = await self._database_now()

        async def run_worker():
            async with self.sessions() as session:
                result = await process_active_schedules(session, now=now)
                await session.commit()
                return result

        await asyncio.wait_for(asyncio.gather(run_worker(), run_worker()), timeout=10)
        async with self.sessions() as session:
            occurrence = await session.scalar(
                select(DoseOccurrence).where(
                    DoseOccurrence.schedule_id == self.schedule_id,
                    DoseOccurrence.scheduled_for == self.scheduled_for,
                )
            )
            self.assertIsNotNone(occurrence)
            self.occurrence_id = occurrence.id
            self.assertEqual(
                await session.scalar(
                    select(func.count())
                    .select_from(DoseOccurrence)
                    .where(DoseOccurrence.schedule_id == self.schedule_id)
                ),
                1,
            )
            self.assertEqual(
                await session.scalar(
                    select(func.count())
                    .select_from(Notification)
                    .where(Notification.related_id == occurrence.id)
                ),
                1,
            )

        times = [now + timedelta(minutes=10), now + timedelta(minutes=15)]
        responses = await asyncio.wait_for(
            asyncio.gather(
                *(
                    self.client.post(
                        f"/api/v1/dose-occurrences/{self.occurrence_id}/snooze",
                        json={"snoozed_until": value.isoformat()},
                    )
                    for value in times
                )
            ),
            timeout=10,
        )
        self.assertEqual([response.status_code for response in responses], [201, 201])
        async with self.sessions() as session:
            occurrence = await session.get(DoseOccurrence, self.occurrence_id)
            self.assertEqual(occurrence.scheduled_for, self.scheduled_for)
            self.assertIn(occurrence.snoozed_until, times)
            self.assertEqual(
                await session.scalar(
                    select(func.count())
                    .select_from(AuditEvent)
                    .where(
                        AuditEvent.target_id == self.occurrence_id,
                        AuditEvent.action == "dose_occurrence.snoozed",
                    )
                ),
                2,
            )

    async def _database_now(self):
        async with self.sessions() as session:
            return await session.scalar(select(func.clock_timestamp()))
