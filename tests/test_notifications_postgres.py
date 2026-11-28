"""Notification API, preference and producer checks on isolated PostgreSQL."""

import asyncio
import os
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, skipUnless
from unittest.mock import patch
from uuid import uuid4

from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database.session import get_session
from app.main import app
from app.modules.audit.models import AuditEvent
from app.modules.identity.auth import get_current_user
from app.modules.identity.models import Role, User, UserRole
from app.modules.identity.request_limits import require_authenticated_rate_limit
from app.modules.medication.enum import DoseStatus, ScheduleStatus
from app.modules.medication.models import DoseOccurrence, MedicationLog, MedicationSchedule
from app.modules.medication.occurrence_worker import process_active_schedules
from app.modules.notifications.models import Notification
from app.modules.prescriptions.core_models import (
    Prescription,
    PrescriptionItem,
    PrescriptionSource,
    PrescriptionStatus,
)
from app.modules.profiles.models import PatientProfile

URL = os.getenv("ML187_TEST_DATABASE_URL")


@skipUnless(URL, "Set ML187_TEST_DATABASE_URL to a disposable migrated PostgreSQL database")
class NotificationsPostgresTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine(URL, hide_parameters=True)
        self.addAsyncCleanup(self.engine.dispose)
        self.connection = await self.engine.connect()
        self.addAsyncCleanup(self.connection.close)
        self.transaction = await self.connection.begin()
        self.addAsyncCleanup(self.transaction.rollback)
        self.db = AsyncSession(
            bind=self.connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        self.addAsyncCleanup(self.db.close)

        self.patient, self.profile = await self.create_patient()
        self.other, _other_profile = await self.create_patient()
        await self.db.commit()
        self.actor = self.patient

        async def session_override():
            async with AsyncSession(
                bind=self.connection,
                expire_on_commit=False,
                join_transaction_mode="create_savepoint",
            ) as session:
                yield session

        async def actor_override():
            return self.actor

        overrides = patch.dict(
            app.dependency_overrides,
            {
                get_session: session_override,
                get_current_user: actor_override,
                require_authenticated_rate_limit: lambda: None,
            },
        )
        overrides.start()
        self.addCleanup(overrides.stop)
        self.client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        self.addAsyncCleanup(self.client.aclose)

    async def create_patient(self):
        role = await self.db.scalar(select(Role).where(Role.code == "patient"))
        if role is None:
            role = Role(code="patient", label=f"ML-187 synthetic patient {uuid4()}")
            self.db.add(role)
            await self.db.flush()

        user = User(
            firebase_uid=f"ml187-{uuid4()}",
            display_name="ML-187 synthetic patient",
            status="active",
        )
        self.db.add(user)
        await self.db.flush()
        self.db.add(UserRole(user_id=user.id, role_id=role.id))
        profile = PatientProfile(
            user_id=user.id,
            date_of_birth=date(1990, 1, 1),
            sex="female",
        )
        self.db.add(profile)
        await self.db.flush()
        return user, profile

    async def add_notification(
        self,
        user_id,
        *,
        created_at,
        read_at=None,
        notification_type="test_notice",
    ):
        notification = Notification(
            user_id=user_id,
            notification_type=notification_type,
            title="Synthetic notification",
            body="Synthetic test data only.",
            related_type=None,
            related_id=None,
            created_at=created_at,
            read_at=read_at,
            dedupe_key=f"ml187-test:{uuid4()}",
        )
        self.db.add(notification)
        await self.db.flush()
        return notification

    async def test_list_filters_orders_pages_and_isolates_accounts(self):
        now = datetime.now(UTC).replace(microsecond=0)
        oldest = await self.add_notification(
            self.patient.id,
            created_at=now - timedelta(minutes=3),
        )
        read = await self.add_notification(
            self.patient.id,
            created_at=now - timedelta(minutes=2),
            read_at=now - timedelta(minutes=1),
        )
        newest = await self.add_notification(
            self.patient.id,
            created_at=now - timedelta(minutes=1),
        )
        other = await self.add_notification(
            self.other.id,
            created_at=now,
        )
        await self.db.commit()

        first_page = await self.client.get(
            "/api/v1/me/notifications?unread=true&page=1&page_size=1"
        )
        self.assertEqual(first_page.status_code, 200, first_page.text)
        self.assertEqual(first_page.headers["Cache-Control"], "no-store")
        self.assertEqual(first_page.json()["total"], 2)
        self.assertEqual(first_page.json()["items"][0]["id"], str(newest.id))
        self.assertEqual(first_page.json()["items"][0]["user_id"], str(self.patient.id))

        second_page = await self.client.get(
            "/api/v1/me/notifications?unread=true&page=2&page_size=1"
        )
        self.assertEqual(second_page.status_code, 200, second_page.text)
        self.assertEqual(second_page.json()["items"][0]["id"], str(oldest.id))

        read_only = await self.client.get("/api/v1/me/notifications?unread=false")
        self.assertEqual(read_only.status_code, 200, read_only.text)
        self.assertEqual(read_only.json()["total"], 1)
        self.assertEqual(read_only.json()["items"][0]["id"], str(read.id))

        all_owned = await self.client.get("/api/v1/me/notifications")
        self.assertEqual(all_owned.status_code, 200, all_owned.text)
        self.assertEqual(all_owned.json()["total"], 3)
        self.assertEqual(
            [item["id"] for item in all_owned.json()["items"]],
            [str(newest.id), str(read.id), str(oldest.id)],
        )

        self.actor = self.other
        other_only = await self.client.get("/api/v1/me/notifications")
        self.assertEqual(other_only.status_code, 200, other_only.text)
        self.assertEqual(other_only.json()["total"], 1)
        self.assertEqual(other_only.json()["items"][0]["id"], str(other.id))

        empty_user, _profile = await self.create_patient()
        self.actor = empty_user
        empty = await self.client.get("/api/v1/me/notifications")
        self.assertEqual(empty.status_code, 200, empty.text)
        self.assertEqual(empty.json(), {"items": [], "page": 1, "page_size": 20, "total": 0})

    async def test_read_is_retry_safe_conceals_others_and_appends_audit(self):
        now = datetime.now(UTC).replace(microsecond=0)
        own = await self.add_notification(self.patient.id, created_at=now)
        foreign = await self.add_notification(self.other.id, created_at=now)
        await self.db.commit()

        first = await self.client.post(f"/api/v1/notifications/{own.id}/read")
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.headers["Cache-Control"], "no-store")
        self.assertIsNotNone(first.json()["read_at"])

        retry = await self.client.post(f"/api/v1/notifications/{own.id}/read")
        self.assertEqual(retry.status_code, 200, retry.text)
        self.assertEqual(retry.json()["read_at"], first.json()["read_at"])

        self.actor = self.other
        hidden = await self.client.post(f"/api/v1/notifications/{own.id}/read")
        self.assertEqual(hidden.status_code, 404, hidden.text)
        self.assertEqual(hidden.json()["code"], "RESOURCE_NOT_FOUND")
        self.assertNotIn(str(own.id), hidden.text)

        self.actor = self.patient
        audit_events = list(
            (
                await self.db.scalars(
                    select(AuditEvent).where(
                        AuditEvent.action == "notification.read",
                        AuditEvent.target_type == "notification",
                        AuditEvent.target_id == own.id,
                    )
                )
            ).all()
        )
        self.assertEqual(len(audit_events), 1)
        self.assertEqual(audit_events[0].actor_user_id, self.patient.id)
        self.assertEqual(audit_events[0].outcome, "success")

        self.actor = self.other
        foreign_read = await self.client.post(f"/api/v1/notifications/{foreign.id}/read")
        self.assertEqual(foreign_read.status_code, 200, foreign_read.text)

    async def test_preferences_are_persisted_without_mutating_doses_and_gate_reminders(self):
        now = datetime.now(UTC).replace(microsecond=0)
        prescription = Prescription(
            patient_user_id=self.patient.id,
            source=PrescriptionSource.PATIENT,
            created_by=self.patient.id,
            status=PrescriptionStatus.ACTIVE,
            issued_at=now - timedelta(days=2),
            starts_on=now.date() - timedelta(days=1),
            ends_on=now.date() + timedelta(days=5),
        )
        self.db.add(prescription)
        await self.db.flush()
        item = PrescriptionItem(
            prescription_id=prescription.id,
            medicine_name_snapshot="Synthetic ML-187 medicine",
            dose_text="Synthetic dose",
            frequency="daily",
            instructions="Synthetic test data only.",
            duration_days=7,
            is_prn=False,
        )
        self.db.add(item)
        await self.db.flush()

        schedule = MedicationSchedule(
            prescription_item_id=item.id,
            patient_user_id=self.patient.id,
            timezone="UTC",
            starts_at=now - timedelta(hours=3),
            ends_at=now + timedelta(days=4),
            confirmed_at=now - timedelta(hours=2),
            status=ScheduleStatus.ACTIVE,
            occurrences_generated_through=now + timedelta(days=5),
        )
        self.db.add(schedule)
        await self.db.flush()

        first_occurrence = DoseOccurrence(
            schedule_id=schedule.id,
            scheduled_for=now - timedelta(minutes=30),
            status=DoseStatus.UNREPORTED,
        )
        self.db.add(first_occurrence)
        await self.db.commit()

        processed = await process_active_schedules(self.db, now=now)
        await self.db.commit()
        self.assertGreaterEqual(processed, 1)
        first_notification = await self.db.scalar(
            select(Notification).where(
                Notification.related_type == "dose_occurrence",
                Notification.related_id == first_occurrence.id,
                Notification.notification_type == "dose_reminder",
            )
        )
        self.assertIsNotNone(first_notification)

        second_occurrence = DoseOccurrence(
            schedule_id=schedule.id,
            scheduled_for=now - timedelta(minutes=20),
            status=DoseStatus.UNREPORTED,
        )
        self.db.add(second_occurrence)
        await self.db.commit()

        before = await self.dose_snapshot(schedule.id)
        denominator_before = await self.dose_count(schedule.id)
        logs_before = await self.db.scalar(select(func.count()).select_from(MedicationLog))

        changed = await self.client.patch(
            "/api/v1/me/patient-profile",
            json={
                "notification_preferences": {
                    "medication_in_app": False,
                    "vaccination_in_app": False,
                }
            },
        )
        self.assertEqual(changed.status_code, 200, changed.text)
        self.assertEqual(
            changed.json()["notification_preferences"],
            {"medication_in_app": False, "vaccination_in_app": False},
        )

        reloaded = await self.client.get("/api/v1/me")
        self.assertEqual(reloaded.status_code, 200, reloaded.text)
        self.assertEqual(
            reloaded.json()["patient_profile"]["notification_preferences"],
            {"medication_in_app": False, "vaccination_in_app": False},
        )
        self.assertEqual(await self.dose_snapshot(schedule.id), before)
        self.assertEqual(await self.dose_count(schedule.id), denominator_before)
        self.assertEqual(
            await self.db.scalar(select(func.count()).select_from(MedicationLog)),
            logs_before,
        )

        profile = await self.db.get(PatientProfile, self.patient.id)
        await self.db.refresh(profile)
        processed_again = await process_active_schedules(self.db, now=now)
        await self.db.commit()
        self.assertEqual(processed_again, 0)
        second_notification = await self.db.scalar(
            select(Notification.id).where(
                Notification.related_type == "dose_occurrence",
                Notification.related_id == second_occurrence.id,
                Notification.notification_type == "dose_reminder",
            )
        )
        self.assertIsNone(second_notification)
        self.assertEqual(await self.dose_snapshot(schedule.id), before)
        self.assertEqual(await self.dose_count(schedule.id), denominator_before)

        enabled = await self.client.patch(
            "/api/v1/me/patient-profile",
            json={"notification_preferences": {"medication_in_app": True}},
        )
        self.assertEqual(enabled.status_code, 200, enabled.text)
        profile = await self.db.get(PatientProfile, self.patient.id)
        await self.db.refresh(profile)
        await process_active_schedules(self.db, now=now)
        await self.db.commit()
        second_notification = await self.db.scalar(
            select(Notification.id).where(
                Notification.related_type == "dose_occurrence",
                Notification.related_id == second_occurrence.id,
                Notification.notification_type == "dose_reminder",
            )
        )
        self.assertIsNotNone(second_notification)
        self.assertEqual(await self.dose_snapshot(schedule.id), before)
        listed = await self.client.get("/api/v1/me/notifications?unread=true")
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual(listed.json()["total"], 2)
        self.assertEqual(
            {item["related_id"] for item in listed.json()["items"]},
            {str(first_occurrence.id), str(second_occurrence.id)},
        )

    async def dose_snapshot(self, schedule_id):
        rows = await self.db.scalars(
            select(DoseOccurrence)
            .where(DoseOccurrence.schedule_id == schedule_id)
            .order_by(DoseOccurrence.scheduled_for, DoseOccurrence.id)
            .execution_options(populate_existing=True)
        )
        return [(row.id, row.scheduled_for, row.status, row.snoozed_until) for row in rows.all()]

    async def dose_count(self, schedule_id):
        return await self.db.scalar(
            select(func.count())
            .select_from(DoseOccurrence)
            .where(DoseOccurrence.schedule_id == schedule_id)
        )


@skipUnless(URL, "Set ML187_TEST_DATABASE_URL to a disposable migrated PostgreSQL database")
class NotificationReadConcurrencyPostgresTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine(URL, hide_parameters=True)
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)
        self.user_id = uuid4()
        self.notification_id = uuid4()
        async with self.session_factory() as session:
            user = User(
                id=self.user_id,
                firebase_uid=f"ml187-concurrent-{uuid4()}",
                display_name="ML-187 synthetic concurrent reader",
                status="active",
            )
            session.add(user)
            await session.flush()
            session.add(
                Notification(
                    id=self.notification_id,
                    user_id=self.user_id,
                    notification_type="test_notice",
                    title="Synthetic concurrent notification",
                    body="Synthetic test data only.",
                    dedupe_key=f"ml187-concurrent:{uuid4()}",
                )
            )
            await session.commit()

        self.actor = SimpleNamespace(id=self.user_id, status="active")

        async def session_override():
            async with self.session_factory() as session:
                yield session

        async def actor_override():
            return self.actor

        self.overrides = patch.dict(
            app.dependency_overrides,
            {
                get_session: session_override,
                get_current_user: actor_override,
                require_authenticated_rate_limit: lambda: None,
            },
        )
        self.overrides.start()
        self.client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()
        self.overrides.stop()
        # The notification.read audit event is append-only. The entire database
        # is disposable and is dropped by the test run cleanup instead.
        await self.engine.dispose()

    async def test_concurrent_read_requests_set_one_timestamp_and_one_transition_event(self):
        path = f"/api/v1/notifications/{self.notification_id}/read"
        first, second = await asyncio.gather(
            self.client.post(path),
            self.client.post(path),
        )
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(first.json()["read_at"], second.json()["read_at"])

        async with self.session_factory() as session:
            count = await session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(
                    AuditEvent.action == "notification.read",
                    AuditEvent.target_id == self.notification_id,
                )
            )
        self.assertEqual(count, 1)
