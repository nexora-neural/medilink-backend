"""ML-64 concurrent start test against a dedicated migrated test database."""

import asyncio
import os
from datetime import UTC, date, datetime, timedelta
from unittest import IsolatedAsyncioTestCase, skipUnless
from unittest.mock import patch
from uuid import UUID, uuid4

from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database.session import get_session
from app.main import app
from app.modules.audit.models import AuditEvent
from app.modules.consent.models import AccessGrant, GrantCategory
from app.modules.consultations import repository
from app.modules.consultations.models import Consultation
from app.modules.identity.auth import get_current_user
from app.modules.identity.models import Role, User, UserRole
from app.modules.identity.request_limits import require_authenticated_rate_limit
from app.modules.profiles.models import PatientProfile, ProfessionalProfile

TEST_DATABASE_URL = os.getenv("ML64_TEST_DATABASE_URL")


@skipUnless(TEST_DATABASE_URL, "Set ML64_TEST_DATABASE_URL to a dedicated test database")
class ConsultationConcurrencyTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine(TEST_DATABASE_URL)
        self.addAsyncCleanup(self.engine.dispose)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

        async with self.sessions() as session:
            roles = {}
            for code in ("doctor", "patient"):
                role = await session.scalar(select(Role).where(Role.code == code))
                if role is None:
                    role = Role(code=code, label=code)
                    session.add(role)
                    await session.flush()
                roles[code] = role.id

            self.doctor = User(
                id=uuid4(),
                firebase_uid=f"ml64-concurrent-doctor-{uuid4()}",
                display_name="ML-64 synthetic concurrency doctor",
                status="active",
            )
            patient = User(
                id=uuid4(),
                firebase_uid=f"ml64-concurrent-patient-{uuid4()}",
                display_name="ML-64 synthetic concurrency patient",
                status="active",
            )
            session.add_all([self.doctor, patient])
            await session.flush()

            session.add_all(
                [
                    UserRole(user_id=self.doctor.id, role_id=roles["doctor"]),
                    UserRole(user_id=patient.id, role_id=roles["patient"]),
                    ProfessionalProfile(
                        user_id=self.doctor.id,
                        role="doctor",
                        doctor_reference=f"ml64-concurrent-{uuid4()}",
                        verification_status="approved",
                    ),
                    PatientProfile(
                        user_id=patient.id,
                        date_of_birth=date(1990, 1, 1),
                        sex="female",
                    ),
                ]
            )
            await session.flush()

            now = datetime.now(UTC)
            self.grant_id = uuid4()
            session.add(
                AccessGrant(
                    id=self.grant_id,
                    patient_user_id=patient.id,
                    professional_user_id=self.doctor.id,
                    organization_id=None,
                    allow_consultation=True,
                    status="active",
                    starts_at=now - timedelta(minutes=5),
                    expires_at=now + timedelta(hours=1),
                )
            )
            await session.flush()
            session.add(GrantCategory(grant_id=self.grant_id, category="prescriptions"))
            await session.commit()

        async def session_override():
            async with self.sessions() as session:
                yield session

        async def actor_override():
            return self.doctor

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

        # Committed synthetic fixtures and append-only audit events remain
        # in the dedicated test database, as in the membership concurrency tests.

    async def test_concurrent_starts_create_one_consultation_and_one_audit_event(self):
        original_lock = repository.lock_doctor_grant
        both_requests_ready = asyncio.Event()
        arrivals = 0

        async def synchronized_lock(*args, **kwargs):
            nonlocal arrivals
            arrivals += 1
            if arrivals == 2:
                both_requests_ready.set()
            await asyncio.wait_for(both_requests_ready.wait(), timeout=10)
            return await original_lock(*args, **kwargs)

        payload = {"grant_id": str(self.grant_id)}
        with patch.object(repository, "lock_doctor_grant", synchronized_lock):
            responses = await asyncio.wait_for(
                asyncio.gather(
                    self.client.post("/api/v1/consultations", json=payload),
                    self.client.post("/api/v1/consultations", json=payload),
                ),
                timeout=20,
            )

        self.assertEqual(
            sorted(response.status_code for response in responses),
            [201, 409],
            [response.text for response in responses],
        )
        conflict = next(response for response in responses if response.status_code == 409)
        self.assertEqual(conflict.json()["code"], "INVALID_STATE")
        created = next(response for response in responses if response.status_code == 201)
        consultation_id = UUID(created.json()["id"])

        async with self.sessions() as session:
            consultations = (
                await session.scalars(
                    select(Consultation).where(Consultation.grant_id == self.grant_id)
                )
            ).all()
            self.assertEqual(len(consultations), 1)
            self.assertEqual(consultations[0].id, consultation_id)
            self.assertIsNone(consultations[0].ended_at)

            event_count = await session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(
                    AuditEvent.target_id == consultation_id,
                    AuditEvent.action == "consultation.started",
                )
            )
            self.assertEqual(event_count, 1)
