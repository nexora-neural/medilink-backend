"""ML-65 grant lifecycle tests against an isolated PostgreSQL database."""

import os
from datetime import date, datetime, timedelta
from unittest import IsolatedAsyncioTestCase, skipUnless
from unittest.mock import patch
from uuid import UUID, uuid4

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.database.session import get_session
from app.main import app
from app.modules.audit.models import AuditEvent
from app.modules.consent.models import AccessGrant, GrantCategory
from app.modules.identity.auth import get_current_user
from app.modules.identity.models import Role, User, UserRole
from app.modules.identity.request_limits import require_authenticated_rate_limit
from app.modules.profiles.models import PatientProfile, ProfessionalProfile

TEST_DATABASE_URL = os.getenv("ML65_TEST_DATABASE_URL")


@skipUnless(TEST_DATABASE_URL, "Set ML65_TEST_DATABASE_URL to an isolated migrated database")
class AccessGrantPostgresTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine(TEST_DATABASE_URL)
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
        self.patient = await self.create_user("patient")
        self.doctor = await self.create_user("doctor")
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
            await self.db.flush()
        user = User(
            firebase_uid=f"ml65-{uuid4()}",
            display_name=f"ML-65 synthetic {code}",
            status="active",
        )
        self.db.add(user)
        await self.db.flush()
        self.db.add(UserRole(user_id=user.id, role_id=role.id))
        if code == "patient":
            self.db.add(
                PatientProfile(user_id=user.id, date_of_birth=date(1990, 1, 1), sex="female")
            )
        else:
            self.doctor_reference = f"ml65-{uuid4()}"
            self.db.add(
                ProfessionalProfile(
                    user_id=user.id,
                    role="doctor",
                    doctor_reference=self.doctor_reference,
                    verification_status="approved",
                )
            )
        await self.db.flush()
        return user

    async def test_create_refresh_and_revoke_persist_grant_and_audit(self):
        response = await self.client.post(
            "/api/v1/access-grants",
            json={
                "doctor_reference": self.doctor_reference,
                "organization_id": None,
                "categories": ["prescriptions", "reports"],
                "duration_hours": 168,
                "allow_consultation": True,
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        created = response.json()
        grant_id = UUID(created["grant"]["id"])
        self.assertEqual(created["categories"], ["prescriptions", "reports"])
        starts_at = datetime.fromisoformat(created["grant"]["starts_at"])
        expires_at = datetime.fromisoformat(created["grant"]["expires_at"])
        self.assertEqual(expires_at - starts_at, timedelta(days=7))
        self.assertTrue(created["grant"]["allow_consultation"])

        # A separate request session reloads the persisted grant, as a page refresh does.
        listing = await self.client.get("/api/v1/me/access-grants?page=1&page_size=100")
        self.assertEqual(listing.status_code, 200, listing.text)
        self.assertEqual(listing.json()["total"], 1)
        self.assertEqual(
            listing.json()["items"][0]["doctor_display_name"], "ML-65 synthetic doctor"
        )
        self.assertEqual(listing.json()["items"][0]["grant"]["id"], str(grant_id))
        self.assertEqual(
            listing.json()["items"][0]["categories"], ["prescriptions", "reports"]
        )

        persisted = await self.db.get(AccessGrant, grant_id)
        self.assertEqual(persisted.patient_user_id, self.patient.id)
        self.assertEqual(persisted.professional_user_id, self.doctor.id)
        categories = list(
            await self.db.scalars(
                select(GrantCategory.category).where(GrantCategory.grant_id == grant_id)
            )
        )
        self.assertEqual(sorted(categories), ["prescriptions", "reports"])

        revoked = await self.client.post(
            f"/api/v1/access-grants/{grant_id}/revoke",
            json={"reason": "Patient requested revocation"},
        )
        self.assertEqual(revoked.status_code, 201, revoked.text)
        self.assertEqual(revoked.json()["grant"]["status"], "revoked")
        self.assertEqual(revoked.json()["categories"], ["prescriptions", "reports"])

        refreshed = await self.client.get("/api/v1/me/access-grants")
        self.assertEqual(refreshed.status_code, 200, refreshed.text)
        self.assertEqual(refreshed.json()["items"][0]["grant"]["status"], "revoked")
        self.assertEqual(
            refreshed.json()["items"][0]["doctor_display_name"], "ML-65 synthetic doctor"
        )
        persisted = await self.db.get(AccessGrant, grant_id)
        await self.db.refresh(persisted)
        self.assertEqual(persisted.status, "revoked")
        self.assertIsNotNone(persisted.revoked_at)
        self.assertEqual(persisted.revocation_reason, "Patient requested revocation")

        events = list(
            await self.db.scalars(
                select(AuditEvent)
                .where(AuditEvent.target_id == grant_id)
                .order_by(AuditEvent.action)
            )
        )
        self.assertEqual(
            {event.action for event in events},
            {"access_grant.created", "access_grant.revoked"},
        )
        self.assertTrue(all(event.actor_user_id == self.patient.id for event in events))
