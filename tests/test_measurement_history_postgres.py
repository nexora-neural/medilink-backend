"""History API integration against a dedicated, migrated PostgreSQL database.

Only Firebase identity verification is substituted. Authorization, queries,
creation, corrections and response serialization execute through the real API.
"""

import os
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, skipUnless
from unittest.mock import patch
from uuid import UUID, uuid4

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.database.session import get_session
from app.main import app
from app.modules.consent.models import AccessGrant, GrantCategory
from app.modules.identity.auth import get_verified_identity
from app.modules.identity.models import Role, User, UserRole
from app.modules.measurements.models import Measurement
from app.modules.profiles.models import PatientProfile, ProfessionalProfile

TEST_DATABASE_URL = os.getenv("ML102_TEST_DATABASE_URL")


@skipUnless(TEST_DATABASE_URL, "Set ML102_TEST_DATABASE_URL to a dedicated migrated test database")
class MeasurementHistoryPostgresTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine(TEST_DATABASE_URL)
        self.addAsyncCleanup(self.engine.dispose)
        self.connection = await self.engine.connect()
        self.addAsyncCleanup(self.connection.close)
        self.transaction = await self.connection.begin()
        self.addAsyncCleanup(self.transaction.rollback)
        self.session = AsyncSession(
            bind=self.connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )
        self.addAsyncCleanup(self.session.close)
        self.roles = {}
        for code in ("patient", "doctor"):
            role = await self.session.scalar(select(Role).where(Role.code == code))
            if role is None:
                role = Role(code=code, label=code)
                self.session.add(role)
                await self.session.flush()
            self.roles[code] = role.id
        self.patient = await self.create_actor("patient")
        self.other = await self.create_actor("patient")
        self.doctor = await self.create_actor("doctor")
        await self.session.commit()
        self.actor = self.patient
        self.base = f"/api/v1/patients/{self.patient.id}/measurements"

        async def session_override():
            yield self.session

        async def identity_override():
            return {"uid": self.actor.firebase_uid}

        overrides = patch.dict(app.dependency_overrides, {
            get_session: session_override,
            get_verified_identity: identity_override,
        })
        overrides.start()
        self.addCleanup(overrides.stop)
        self.client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        self.addAsyncCleanup(self.client.aclose)

    async def create_actor(self, role):
        user = User(firebase_uid=str(uuid4()), display_name="Synthetic ML102", status="active")
        self.session.add(user)
        await self.session.flush()
        self.session.add(UserRole(user_id=user.id, role_id=self.roles[role]))
        if role == "patient":
            self.session.add(PatientProfile(
                user_id=user.id, date_of_birth=date(1990, 1, 1), sex="female"
            ))
        else:
            self.session.add(ProfessionalProfile(
                user_id=user.id, role="doctor", doctor_reference=str(uuid4()),
                verification_status="approved",
            ))
        await self.session.flush()
        return SimpleNamespace(id=user.id, firebase_uid=user.firebase_uid)

    async def grant_doctor(self):
        grant = AccessGrant(
            patient_user_id=self.patient.id, professional_user_id=self.doctor.id,
            starts_at=datetime.now(UTC) - timedelta(hours=1),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        self.session.add(grant)
        await self.session.flush()
        self.session.add(GrantCategory(grant_id=grant.id, category="measurements_vaccinations"))
        await self.session.commit()

    async def test_original_successive_corrections_and_pagination(self):
        measured_at = "2025-01-01T04:00:00Z"
        created = await self.client.post(self.base, json={
            "type": "weight", "value": 140, "unit": "lb", "measured_at": measured_at,
        })
        self.assertEqual(created.status_code, 201, created.text)
        original = created.json()
        correction_ids = [original["id"]]
        for value in (141, 142):
            response = await self.client.post(
                f"/api/v1/measurements/{correction_ids[-1]}/corrections",
                headers={"Idempotency-Key": str(uuid4())},
                json={"value": value, "unit": "lb", "measured_at": measured_at,
                      "reason": f"Synthetic correction {value}"},
            )
            self.assertEqual(response.status_code, 201, response.text)
            correction_ids.append(response.json()["id"])
        # Equal timestamps exercise the deterministic ID tie-breaker across pages.
        timestamp = datetime(2025, 1, 2, tzinfo=UTC)
        for record_id in correction_ids:
            row = await self.session.get(Measurement, UUID(record_id))
            row.created_at = timestamp
        await self.session.commit()
        all_items = []
        for page in (1, 2, 3):
            response = await self.client.get(
                self.base + "/history", params={"page": page, "page_size": 1}
            )
            self.assertEqual(response.status_code, 200, response.text)
            body = response.json()
            self.assertEqual((body["page"], body["page_size"], body["total"]), (page, 1, 3))
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            all_items.extend(body["items"])
        self.assertEqual([r["id"] for r in all_items], sorted(correction_ids, reverse=True))
        by_id = {r["id"]: r for r in all_items}
        for index, record_id in enumerate(correction_ids):
            row = by_id[record_id]
            self.assertEqual(row["value"], 140 + index)
            self.assertEqual(row["unit"], "lb")
            self.assertEqual(row["measured_at"], original["measured_at"])
            self.assertEqual(row["source"], "patient")
            self.assertEqual(row["recorded_by"], str(self.patient.id))
            self.assertEqual(row["supersedes_id"], correction_ids[index - 1] if index else None)
            self.assertEqual(row["status"], "active" if index == 2 else "superseded")
            self.assertEqual(
                row["correction_reason"], f"Synthetic correction {140 + index}" if index else None
            )
        beyond = await self.client.get(self.base + "/history", params={"page": 4, "page_size": 1})
        self.assertEqual(beyond.json()["items"], [])
        self.assertEqual(beyond.json()["total"], 3)

    async def test_empty_and_invalid_pagination(self):
        response = await self.client.get(self.base + "/history")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"items": [], "page": 1, "page_size": 20, "total": 0})
        for params in ({"page": 0}, {"page_size": 0}, {"page_size": 101}):
            with self.subTest(params=params):
                response = await self.client.get(self.base + "/history", params=params)
                self.assertEqual(response.status_code, 422, response.text)

    async def test_unauthenticated_and_cross_patient_denied(self):
        identity = app.dependency_overrides.pop(get_verified_identity)
        try:
            response = await self.client.get(self.base + "/history")
            self.assertEqual(response.status_code, 401, response.text)
        finally:
            app.dependency_overrides[get_verified_identity] = identity
        self.actor = self.other
        response = await self.client.get(self.base + "/history")
        self.assertEqual(response.status_code, 404, response.text)
        self.assertNotIn("items", response.json())

    async def test_doctor_grant_and_revoked_roles(self):
        self.actor = self.doctor
        response = await self.client.get(self.base + "/history")
        self.assertEqual(response.status_code, 404, response.text)
        await self.grant_doctor()
        response = await self.client.get(self.base + "/history")
        self.assertEqual(response.status_code, 200, response.text)
        for actor, role in ((self.doctor, "doctor"), (self.patient, "patient")):
            self.actor = actor
            assignment = await self.session.get(UserRole, (actor.id, self.roles[role]))
            assignment.revoked_at = datetime.now(UTC)
            await self.session.commit()
            response = await self.client.get(self.base + "/history")
            self.assertEqual(response.status_code, 404, response.text)
            self.assertNotIn("items", response.json())
