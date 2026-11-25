"""Concurrent issuance using separate PostgreSQL connections."""

import asyncio
import os
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, skipUnless
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import models as _models  # noqa: F401
from app.modules.audit.models import AuditEvent
from app.modules.consent.models import AccessGrant, GrantCategory
from app.modules.consultations.models import Consultation
from app.modules.identity.models import Role, User, UserRole
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
                starts_on=date(2026, 11, 25),
                ends_on=date(2026, 11, 27),
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
                    frequency="Synthetic frequency",
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
