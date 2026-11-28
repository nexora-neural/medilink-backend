"""Synthetic rollback-isolated fixtures shared by ML-154 database tests."""

import os
from datetime import UTC, datetime, timedelta
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.database.session import get_session
from app.main import app
from app.modules.consent.models import AccessGrant, GrantCategory
from app.modules.consultations.models import Consultation
from app.modules.identity.auth import get_current_user
from app.modules.identity.request_limits import require_authenticated_rate_limit
from app.modules.prescriptions import repository
from app.modules.prescriptions.core_models import PrescriptionItem
from app.modules.prescriptions.schemas import PrescriptionInput
from tests.test_ml155_schedules_postgres import SchedulePostgresTests as _UserFactory

URL = os.getenv("ML154_TEST_DATABASE_URL")


class LifecycleFixture(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        engine = create_async_engine(URL)
        self.addAsyncCleanup(engine.dispose)
        connection = await engine.connect()
        self.addAsyncCleanup(connection.close)
        transaction = await connection.begin()
        self.addAsyncCleanup(transaction.rollback)
        self.db = AsyncSession(
            bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )
        self.addAsyncCleanup(self.db.close)
        self.patient = await _UserFactory.user(self, "patient")
        self.other = await _UserFactory.user(self, "patient")
        self.doctor = await _UserFactory.user(self, "doctor")
        self.admin = await _UserFactory.user(self, "super_admin")
        self.now = datetime.now(UTC)
        self.grant = AccessGrant(
            patient_user_id=self.patient.id,
            professional_user_id=self.doctor.id,
            allow_consultation=True,
            status="active",
            starts_at=self.now - timedelta(hours=1),
            expires_at=self.now + timedelta(hours=1),
        )
        self.db.add(self.grant)
        await self.db.flush()
        self.db.add(GrantCategory(grant_id=self.grant.id, category="prescriptions"))
        self.consult = Consultation(
            patient_user_id=self.patient.id,
            doctor_user_id=self.doctor.id,
            grant_id=self.grant.id,
            started_at=self.now - timedelta(minutes=20),
        )
        self.db.add(self.consult)
        await self.db.flush()
        self.actor = self.patient
        self.rx = await self.make_rx()
        self.rx_id = self.rx.id
        self.patient_id = self.patient.id
        await self.db.commit()

        async def session():
            try:
                yield self.db
            finally:
                for row in list(self.db.identity_map.values()):
                    await self.db.refresh(row)

        overrides = patch.dict(
            app.dependency_overrides,
            {
                get_session: session,
                get_current_user: lambda: self.actor,
                require_authenticated_rate_limit: lambda: None,
            },
        )
        overrides.start()
        self.addCleanup(overrides.stop)
        self.client = AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
        )
        self.addAsyncCleanup(self.client.aclose)
        self.path = f"/api/v1/prescriptions/{self.rx.id}"

    def payload(self, doctor=False, reason="Private synthetic correction"):
        body = dict(
            reason=reason,
            claimed_prescriber="Unverified label",
            starts_on=self.now.date().isoformat(),
            ends_on=(self.now + timedelta(days=5)).date().isoformat(),
            items=[
                dict(
                    medicine_name_snapshot="Synthetic medicine",
                    dose_text="Original dose",
                    frequency="daily",
                    route="Oral",
                    duration_days=6,
                    quantity=6,
                    quantity_unit="tablet",
                    is_prn=False,
                    instructions="Original instructions",
                )
            ],
        )
        if doctor:
            body["consultation_id"] = str(self.consult.id)
        return body

    async def make_rx(self, doctor=False, status="active"):
        payload = PrescriptionInput.model_validate(
            {k: v for k, v in self.payload(doctor).items() if k != "reason"}
        )
        rx = await repository.create_prescription_draft(
            self.db,
            patient_id=self.patient.id,
            actor_id=self.doctor.id if doctor else self.patient.id,
            source="doctor" if doctor else "patient",
            payload=payload,
        )
        rx.status = status
        rx.issued_at = self.now - timedelta(days=1) if status != "draft" else None
        await self.db.flush()
        return rx

    async def first_item(self, prescription_id=None):
        return await self.db.scalar(
            select(PrescriptionItem).where(
                PrescriptionItem.prescription_id == (prescription_id or self.rx_id)
            )
        )
