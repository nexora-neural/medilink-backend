"""ML-64 regression tests against migrated PostgreSQL."""

import os
from datetime import UTC, date, datetime, timedelta
from unittest import IsolatedAsyncioTestCase, skipUnless
from unittest.mock import patch
from uuid import UUID, uuid4

from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.database.session import get_session
from app.main import app
from app.modules.consent.models import AccessGrant, GrantCategory
from app.modules.identity.auth import get_current_user
from app.modules.identity.authorization import require_doctor_grant
from app.modules.identity.models import Role, User, UserRole
from app.modules.identity.request_limits import require_authenticated_rate_limit
from app.modules.organizations.models import Organization, OrganizationMembership
from app.modules.profiles.models import PatientProfile, ProfessionalProfile

TEST_DATABASE_URL = os.getenv("ML64_TEST_DATABASE_URL")


@skipUnless(
    TEST_DATABASE_URL,
    "Set ML64_TEST_DATABASE_URL to a migrated test database",
)
class ConsultationPostgresTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        engine = create_async_engine(TEST_DATABASE_URL)
        self.addAsyncCleanup(engine.dispose)

        connection = await engine.connect()
        self.addAsyncCleanup(connection.close)

        transaction = await connection.begin()
        self.addAsyncCleanup(transaction.rollback)

        self.db = AsyncSession(
            bind=connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        self.addAsyncCleanup(self.db.close)

        self.patient = await self.create_user("patient")
        self.other_patient = await self.create_user("patient")
        self.doctor = await self.create_user("doctor")
        self.other_doctor = await self.create_user("doctor")
        await self.db.commit()

        self.actor = self.doctor

        async def session_override():
            yield self.db

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

        self.client = AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        )
        self.addAsyncCleanup(self.client.aclose)

    async def create_user(self, code):
        role = await self.db.scalar(select(Role).where(Role.code == code))
        if role is None:
            role = Role(code=code, label=code)
            self.db.add(role)

        user = User(
            firebase_uid=f"ml64-test-{uuid4()}",
            display_name=f"ML-64 synthetic {code}",
            status="active",
        )
        self.db.add(user)
        await self.db.flush()

        self.db.add(UserRole(user_id=user.id, role_id=role.id))

        if code == "patient":
            self.db.add(
                PatientProfile(
                    user_id=user.id,
                    date_of_birth=date(1990, 1, 1),
                    sex="female",
                )
            )
        else:
            self.db.add(
                ProfessionalProfile(
                    user_id=user.id,
                    role="doctor",
                    doctor_reference=f"ml64-{uuid4()}",
                    verification_status="approved",
                )
            )

        await self.db.flush()
        return user

    async def create_grant(
        self,
        *,
        doctor=None,
        patient=None,
        allow=True,
        state="active",
    ):
        now = datetime.now(UTC)
        grant = AccessGrant(
            patient_user_id=(patient.id if patient is not None else self.patient.id),
            professional_user_id=(doctor.id if doctor is not None else self.doctor.id),
            organization_id=None,
            allow_consultation=allow,
            status="revoked" if state == "revoked" else "active",
            starts_at=now - timedelta(hours=2),
            expires_at=now + timedelta(hours=1),
            revoked_at=now if state == "revoked" else None,
        )

        if state == "expired":
            grant.expires_at = now - timedelta(hours=1)
        elif state == "future":
            grant.starts_at = now + timedelta(minutes=10)

        self.db.add(grant)
        await self.db.flush()
        self.db.add(GrantCategory(grant_id=grant.id, category="prescriptions"))
        await self.db.commit()
        return grant

    async def start(self, grant):
        return await self.client.post(
            "/api/v1/consultations",
            json={"grant_id": str(grant.id)},
        )

    async def test_queue_filters_other_doctor_expired_revoked_and_future(self):
        active = await self.create_grant()
        read_only = await self.create_grant(allow=False)
        await self.create_grant(doctor=self.other_doctor)
        await self.create_grant(state="expired")
        await self.create_grant(state="revoked")
        await self.create_grant(state="future")

        response = await self.client.get("/api/v1/me/patient-queue")

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["total"], 2)
        self.assertEqual(
            {item["grant_id"] for item in body["items"]},
            {str(active.id), str(read_only.id)},
        )
        entry = next(item for item in body["items"] if item["grant_id"] == str(read_only.id))
        self.assertFalse(entry["allow_consultation"])
        self.assertEqual(entry["categories"], ["prescriptions"])

    async def test_read_only_and_expired_grants_cannot_start(self):
        for state, allow in (
            ("active", False),
            ("expired", True),
            ("revoked", True),
            ("future", True),
        ):
            with self.subTest(state=state, allow=allow):
                grant = await self.create_grant(state=state, allow=allow)
                response = await self.start(grant)
                self.assertEqual(response.status_code, 403, response.text)

    async def test_other_doctor_cannot_start_or_end_consultation(self):
        grant = await self.create_grant()
        response = await self.start(grant)
        self.assertEqual(response.status_code, 201, response.text)
        consultation_id = response.json()["id"]

        self.actor = self.other_doctor

        response = await self.start(grant)
        self.assertEqual(response.status_code, 404, response.text)

        response = await self.client.post(
            f"/api/v1/consultations/{consultation_id}/end",
            json={"summary": "Must not be saved."},
        )
        self.assertEqual(response.status_code, 404, response.text)

        response = await self.client.get(f"/api/v1/patients/{self.patient.id}/consultations")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["items"], [])
        self.assertEqual(response.json()["total"], 0)

    async def test_end_blocks_clinical_writes_and_preserves_first_summary(self):
        grant = await self.create_grant()
        response = await self.start(grant)
        self.assertEqual(response.status_code, 201, response.text)
        consultation_id = UUID(response.json()["id"])

        await require_doctor_grant(
            self.db,
            self.doctor,
            self.patient.id,
            None,
            "prescriptions",
            clinical_write=True,
        )

        response = await self.client.post(
            f"/api/v1/consultations/{consultation_id}/end",
            json={"summary": "Original synthetic summary."},
        )
        self.assertEqual(response.status_code, 201, response.text)
        first = response.json()

        with self.assertRaises(HTTPException) as raised:
            await require_doctor_grant(
                self.db,
                self.doctor,
                self.patient.id,
                None,
                "prescriptions",
                clinical_write=True,
            )
        self.assertEqual(raised.exception.status_code, 403)

        response = await self.client.post(
            f"/api/v1/consultations/{consultation_id}/end",
            json={"summary": "Must not replace the original."},
        )
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["ended_at"], first["ended_at"])
        self.assertEqual(
            response.json()["summary"],
            "Original synthetic summary.",
        )

        self.actor = self.patient
        response = await self.client.get(f"/api/v1/patients/{self.patient.id}/consultations")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["total"], 1)

    async def test_queue_pagination_preserves_total_on_empty_page(self):
        await self.create_grant()

        response = await self.client.get("/api/v1/me/patient-queue?page=2&page_size=1")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["items"], [])
        self.assertEqual(response.json()["total"], 1)

    async def assert_context_is_blocked(self, grant, consultation_id):
        response = await self.client.get("/api/v1/me/patient-queue")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn(
            str(grant.id),
            {item["grant_id"] for item in response.json()["items"]},
        )

        response = await self.start(grant)
        self.assertEqual(response.status_code, 403, response.text)

        response = await self.client.get(f"/api/v1/patients/{self.patient.id}/consultations")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["items"], [])
        self.assertEqual(response.json()["total"], 0)

        response = await self.client.post(
            f"/api/v1/consultations/{consultation_id}/end",
            json={"summary": "Must not be saved."},
        )
        self.assertEqual(response.status_code, 403, response.text)

        with self.assertRaises(HTTPException) as raised:
            await require_doctor_grant(
                self.db,
                self.doctor,
                self.patient.id,
                grant.organization_id,
                "prescriptions",
                clinical_write=True,
            )
        self.assertEqual(raised.exception.status_code, 403)

    async def test_patient_revocation_blocks_existing_context(self):
        grant = await self.create_grant()
        response = await self.start(grant)
        self.assertEqual(response.status_code, 201, response.text)
        consultation_id = response.json()["id"]

        self.actor = self.patient
        response = await self.client.post(
            f"/api/v1/access-grants/{grant.id}/revoke",
            json={"reason": "Synthetic permission revocation test."},
        )
        self.assertEqual(response.status_code, 201, response.text)

        self.actor = self.doctor
        await self.assert_context_is_blocked(grant, consultation_id)

    async def test_expiry_blocks_existing_context(self):
        grant = await self.create_grant()
        response = await self.start(grant)
        self.assertEqual(response.status_code, 201, response.text)
        consultation_id = response.json()["id"]

        grant.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await self.db.commit()

        await self.assert_context_is_blocked(grant, consultation_id)

    async def test_membership_and_organization_suspension_block_context(self):
        organization = Organization(
            name="ML-64 synthetic clinic",
            type="clinic",
            status="active",
        )
        self.db.add(organization)
        await self.db.flush()

        membership = OrganizationMembership(
            organization_id=organization.id,
            user_id=self.doctor.id,
            role="doctor",
            status="approved",
        )
        self.db.add(membership)
        await self.db.commit()

        grant = await self.create_grant()
        grant.organization_id = organization.id
        await self.db.commit()

        response = await self.start(grant)
        self.assertEqual(response.status_code, 201, response.text)
        consultation_id = response.json()["id"]

        membership.status = "suspended"
        await self.db.commit()
        await self.assert_context_is_blocked(grant, consultation_id)

        membership.status = "approved"
        organization.status = "suspended"
        await self.db.commit()
        await self.assert_context_is_blocked(grant, consultation_id)
