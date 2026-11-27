"""Concurrent issuance using separate PostgreSQL connections."""

import asyncio
import os
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, skipUnless
from unittest.mock import patch
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import models as _models  # noqa: F401
from app.modules.audit.models import AuditEvent
from app.modules.consent.models import AccessGrant, GrantCategory
from app.modules.consultations.models import Consultation
from app.modules.identity.auth import AuthenticationError
from app.modules.identity.models import Role, User, UserRole
from app.modules.medication import repository as medication_repository
from app.modules.medication import service as medication_service
from app.modules.medication.models import MedicationSchedule
from app.modules.medication.schemas import ScheduleInput
from app.modules.prescriptions import repository, service
from app.modules.prescriptions.core_models import Prescription, PrescriptionItem
from app.modules.profiles.models import PatientProfile, ProfessionalProfile

TEST_DATABASE_URL = os.getenv("PRESCRIPTION_STORAGE_TEST_DATABASE_URL")


@skipUnless(
    TEST_DATABASE_URL,
    "Set PRESCRIPTION_STORAGE_TEST_DATABASE_URL to a dedicated test database",
)
class PrescriptionIssueConcurrencyTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine(TEST_DATABASE_URL)
        self.addAsyncCleanup(self.engine.dispose)
        self.sessions = async_sessionmaker(
            self.engine,
            expire_on_commit=False,
        )

        async with self.sessions() as session:
            doctor = User(
                firebase_uid=f"issue-doctor-{uuid4()}",
                display_name="Synthetic concurrency doctor",
                status="active",
            )
            patient = User(
                firebase_uid=f"issue-patient-{uuid4()}",
                display_name="Synthetic concurrency patient",
                status="active",
            )
            session.add_all([doctor, patient])
            await session.flush()

            for actor, code in (
                (doctor, "doctor"),
                (patient, "patient"),
            ):
                role = await session.scalar(select(Role).where(Role.code == code))
                if role is None:
                    role = Role(code=code, label=code)
                    session.add(role)
                    await session.flush()

                session.add(UserRole(user_id=actor.id, role_id=role.id))

            session.add_all(
                [
                    PatientProfile(
                        user_id=patient.id,
                        date_of_birth=date(1990, 1, 1),
                        sex="female",
                    ),
                    ProfessionalProfile(
                        user_id=doctor.id,
                        role="doctor",
                        doctor_reference=f"CONCURRENT-{uuid4()}",
                        verification_status="approved",
                    ),
                ]
            )
            await session.flush()

            now = datetime.now(UTC)
            grant = AccessGrant(
                patient_user_id=patient.id,
                professional_user_id=doctor.id,
                status="active",
                allow_consultation=True,
                starts_at=now - timedelta(minutes=10),
                expires_at=now + timedelta(hours=1),
            )
            session.add(grant)
            await session.flush()

            session.add(
                GrantCategory(
                    grant_id=grant.id,
                    category="prescriptions",
                )
            )
            consultation = Consultation(
                patient_user_id=patient.id,
                doctor_user_id=doctor.id,
                grant_id=grant.id,
                started_at=now - timedelta(minutes=5),
            )
            session.add(consultation)
            await session.flush()

            prescription = Prescription(
                patient_user_id=patient.id,
                prescriber_user_id=doctor.id,
                consultation_id=consultation.id,
                source="doctor",
                status="draft",
                starts_on=now.astimezone(ZoneInfo("Asia/Colombo")).date(),
                ends_on=(now.astimezone(ZoneInfo("Asia/Colombo")).date() + timedelta(days=2)),
                created_by=doctor.id,
                version=1,
            )
            session.add(prescription)
            await session.flush()

            session.add(
                PrescriptionItem(
                    prescription_id=prescription.id,
                    medicine_name_snapshot="Synthetic medicine",
                    dose_text="Synthetic dose",
                    route="Oral",
                    frequency="daily",
                    duration_days=3,
                    quantity=3,
                    quantity_unit="tablet",
                    is_prn=False,
                )
            )

            self.doctor_id = doctor.id
            self.prescription_id = prescription.id
            await session.commit()

        # Separate connections need committed fixtures.
        # Synthetic fixtures and audit events remain in the test database.

    async def test_concurrent_issue_creates_one_transition_and_audit(self):
        original_lock = repository.lock_prescription
        both_ready = asyncio.Event()
        arrivals = 0

        async def synchronized_lock(*args, **kwargs):
            nonlocal arrivals
            arrivals += 1
            if arrivals == 2:
                both_ready.set()

            await asyncio.wait_for(both_ready.wait(), timeout=10)
            return await original_lock(*args, **kwargs)

        async def issue_once():
            async with self.sessions() as session:
                doctor = await session.get(User, self.doctor_id)
                request = SimpleNamespace(state=SimpleNamespace(request_id=str(uuid4())))
                return await service.issue_prescription(
                    session=session,
                    user=doctor,
                    request=request,
                    prescription_id=self.prescription_id,
                )

        with patch.object(
            repository,
            "lock_prescription",
            synchronized_lock,
        ):
            results = await asyncio.wait_for(
                asyncio.gather(issue_once(), issue_once()),
                timeout=20,
            )

        self.assertEqual(
            [result.prescription.status for result in results],
            ["active", "active"],
        )
        self.assertEqual(
            [result.prescription.version for result in results],
            [2, 2],
        )
        self.assertEqual(
            results[0].prescription.issued_at,
            results[1].prescription.issued_at,
        )

        async with self.sessions() as session:
            stored = await session.get(Prescription, self.prescription_id)
            event_count = await session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(
                    AuditEvent.target_id == self.prescription_id,
                    AuditEvent.action == "prescription.issued",
                )
            )

            self.assertEqual(stored.status, "active")
            self.assertEqual(stored.version, 2)
            self.assertEqual(event_count, 1)

    async def test_schedule_replacement_racing_issue_leaves_no_active_schedule(self):
        # Prepare an issued predecessor, its schedule and a linked draft.
        async with self.sessions() as session:
            doctor = await session.get(User, self.doctor_id)
            request = SimpleNamespace(state=SimpleNamespace(request_id=str(uuid4())))
            await service.issue_prescription(
                session=session,
                user=doctor,
                request=request,
                prescription_id=self.prescription_id,
            )

            original = await session.get(Prescription, self.prescription_id)
            original_item = await session.scalar(
                select(PrescriptionItem).where(PrescriptionItem.prescription_id == original.id)
            )
            now = datetime.now(UTC)

            schedule = MedicationSchedule(
                prescription_item_id=original_item.id,
                patient_user_id=original.patient_user_id,
                timezone="Asia/Colombo",
                starts_at=now - timedelta(hours=1),
                confirmed_at=now,
                status="active",
                version=1,
            )
            replacement = Prescription(
                patient_user_id=original.patient_user_id,
                prescriber_user_id=self.doctor_id,
                consultation_id=original.consultation_id,
                source="doctor",
                status="draft",
                starts_on=original.starts_on,
                ends_on=original.ends_on,
                previous_prescription_id=original.id,
                created_by=self.doctor_id,
                version=1,
            )
            session.add_all([schedule, replacement])
            await session.flush()

            session.add(
                PrescriptionItem(
                    prescription_id=replacement.id,
                    medicine_name_snapshot="Synthetic replacement",
                    dose_text="Synthetic dose",
                    route="Oral",
                    frequency="daily",
                    duration_days=3,
                    quantity=3,
                    quantity_unit="tablet",
                    is_prn=False,
                )
            )

            schedule_id = schedule.id
            item_id = original_item.id
            patient_id = original.patient_user_id
            replacement_id = replacement.id
            await session.commit()

        payload = ScheduleInput(
            prescription_item_id=item_id,
            timezone="Asia/Colombo",
            starts_at=now + timedelta(minutes=10),
            ends_at=now + timedelta(days=1),
            times=["08:00:00"],
            confirmed=True,
        )

        schedule_locked = asyncio.Event()
        release_replacement = asyncio.Event()
        issue_connected = asyncio.Event()
        issue_pid = None
        real_lock_schedule = medication_repository.lock_schedule

        async def pause_after_schedule_lock(*args, **kwargs):
            row = await real_lock_schedule(*args, **kwargs)
            schedule_locked.set()
            await asyncio.wait_for(release_replacement.wait(), timeout=15)
            return row

        async def replace_schedule():
            async with self.sessions() as session:
                return await medication_service.replace_schedule(
                    session,
                    schedule_id=schedule_id,
                    actor_user_id=patient_id,
                    expected_version=1,
                    payload=payload,
                    request_id=str(uuid4()),
                )

        async def issue_replacement():
            nonlocal issue_pid

            async with self.sessions() as session:
                issue_pid = await session.scalar(select(func.pg_backend_pid()))
                issue_connected.set()

                doctor = await session.get(User, self.doctor_id)
                request = SimpleNamespace(state=SimpleNamespace(request_id=str(uuid4())))
                return await service.issue_prescription(
                    session=session,
                    user=doctor,
                    request=request,
                    prescription_id=replacement_id,
                )

        tasks = []

        with patch.object(
            medication_repository,
            "lock_schedule",
            pause_after_schedule_lock,
        ):
            try:
                replacement_task = asyncio.create_task(replace_schedule())
                tasks.append(replacement_task)

                await asyncio.wait_for(schedule_locked.wait(), timeout=10)

                issue_task = asyncio.create_task(issue_replacement())
                tasks.append(issue_task)
                await asyncio.wait_for(issue_connected.wait(), timeout=10)

                # Verify that issuance actually reaches a PostgreSQL lock wait.
                blocked = False
                async with self.sessions() as observer:
                    for _ in range(100):
                        blockers = await observer.scalar(select(func.pg_blocking_pids(issue_pid)))
                        if blockers:
                            blocked = True
                            break
                        await asyncio.sleep(0.05)

                self.assertTrue(
                    blocked,
                    "Issuance never reached the expected lock wait.",
                )

                release_replacement.set()

                await asyncio.wait_for(
                    asyncio.gather(replacement_task, issue_task),
                    timeout=15,
                )
            finally:
                release_replacement.set()
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

        async with self.sessions() as session:
            original = await session.get(Prescription, self.prescription_id)
            replacement = await session.get(Prescription, replacement_id)
            schedules = list(
                (
                    await session.scalars(
                        select(MedicationSchedule).where(
                            MedicationSchedule.prescription_item_id == item_id
                        )
                    )
                ).all()
            )

            self.assertEqual(original.status, "superseded")
            self.assertEqual(replacement.status, "active")
            self.assertEqual(len(schedules), 2)
            self.assertTrue(
                all(row.status == "cancelled" for row in schedules),
                [row.status for row in schedules],
            )

    async def test_expired_grant_during_lock_wait_denies_issue(self):
        # Prepare an original prescription, its schedule and a replacement draft.
        async with self.sessions() as session:
            draft = await session.get(Prescription, self.prescription_id)
            now = await session.scalar(select(func.clock_timestamp()))

            original = Prescription(
                patient_user_id=draft.patient_user_id,
                prescriber_user_id=self.doctor_id,
                consultation_id=draft.consultation_id,
                source="doctor",
                status="active",
                starts_on=draft.starts_on,
                ends_on=draft.ends_on,
                issued_at=now,
                created_by=self.doctor_id,
                version=1,
            )
            session.add(original)
            await session.flush()

            item = PrescriptionItem(
                prescription_id=original.id,
                medicine_name_snapshot="Synthetic original",
                dose_text="Synthetic dose",
                route="Oral",
                frequency="Daily",
                duration_days=3,
                quantity=3,
                quantity_unit="tablet",
                is_prn=False,
            )
            session.add(item)
            await session.flush()

            schedule = MedicationSchedule(
                prescription_item_id=item.id,
                patient_user_id=draft.patient_user_id,
                timezone="Asia/Colombo",
                starts_at=now,
                confirmed_at=now,
                status="active",
                version=1,
            )
            session.add(schedule)
            draft.previous_prescription_id = original.id

            grant = await session.scalar(
                select(AccessGrant).where(
                    AccessGrant.professional_user_id == self.doctor_id,
                    AccessGrant.patient_user_id == draft.patient_user_id,
                )
            )
            self.assertIsNotNone(grant)

            # The grant is valid initially, but expires during the lock wait.
            expires_at = now + timedelta(seconds=10)
            grant.expires_at = expires_at

            await session.flush()
            original_id = original.id
            schedule_id = schedule.id
            draft_version = draft.version
            await session.commit()

        issue_connected = asyncio.Event()
        issue_pid = None

        async def issue_replacement():
            nonlocal issue_pid

            async with self.sessions() as session:
                issue_pid = await session.scalar(select(func.pg_backend_pid()))
                issue_connected.set()

                doctor = await session.get(User, self.doctor_id)
                request = SimpleNamespace(state=SimpleNamespace(request_id=str(uuid4())))
                return await service.issue_prescription(
                    session=session,
                    user=doctor,
                    request=request,
                    prescription_id=self.prescription_id,
                )

        issue_task = None

        async with self.sessions() as blocker:
            # Hold the original prescription lock in another connection.
            await repository.lock_prescription(blocker, original_id)

            try:
                issue_task = asyncio.create_task(issue_replacement())
                await asyncio.wait_for(issue_connected.wait(), timeout=5)

                async with self.sessions() as observer:
                    blocked = False

                    for _ in range(100):
                        blockers = await observer.scalar(select(func.pg_blocking_pids(issue_pid)))
                        if blockers:
                            blocked = True
                            break
                        await asyncio.sleep(0.05)

                    self.assertTrue(
                        blocked,
                        "Issuance did not reach the expected database lock wait.",
                    )

                    # Wait until the database clock confirms the grant expired.
                    database_now = await observer.scalar(select(func.clock_timestamp()))
                    remaining = (expires_at - database_now).total_seconds()
                    await asyncio.sleep(max(0, remaining) + 0.1)

                    database_now = await observer.scalar(select(func.clock_timestamp()))
                    self.assertGreaterEqual(database_now, expires_at)

                # Issuance can now continue, but permission has expired.
                await blocker.commit()

                with self.assertRaises(AuthenticationError) as caught:
                    await asyncio.wait_for(issue_task, timeout=10)

                self.assertEqual(caught.exception.status_code, 403)
                self.assertEqual(
                    caught.exception.detail["code"],
                    "ACCESS_DENIED",
                )
            finally:
                await blocker.rollback()

                if issue_task is not None:
                    if not issue_task.done():
                        issue_task.cancel()
                    await asyncio.gather(issue_task, return_exceptions=True)

        # A denied request must not change any prescription or schedule.
        async with self.sessions() as session:
            original = await session.get(Prescription, original_id)
            draft = await session.get(Prescription, self.prescription_id)
            schedule = await session.get(MedicationSchedule, schedule_id)

            self.assertEqual(original.status, "active")
            self.assertEqual(original.version, 1)

            self.assertEqual(draft.status, "draft")
            self.assertIsNone(draft.issued_at)
            self.assertEqual(draft.version, draft_version)

            self.assertEqual(schedule.status, "active")
            self.assertEqual(schedule.version, 1)

            event_count = await session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(
                    AuditEvent.target_id.in_([original_id, self.prescription_id]),
                    AuditEvent.action.in_(["prescription.issued", "prescription.superseded"]),
                )
            )
            self.assertEqual(event_count, 0)
