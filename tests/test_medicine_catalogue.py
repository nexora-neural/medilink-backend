"""Integration tests against a separately provisioned PostgreSQL test database."""

import os
from datetime import UTC, datetime, timedelta
from unittest import IsolatedAsyncioTestCase, skipUnless
from uuid import uuid4

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.database.session import get_session
from app.main import app
from app.modules.audit.models import AuditEvent
from app.modules.identity.auth import get_current_user
from app.modules.identity.models import Role, User, UserRole
from app.modules.prescriptions.core_models import Medicine

TEST_DATABASE_URL = os.getenv("ML90_TEST_DATABASE_URL")


@skipUnless(TEST_DATABASE_URL, "Set ML90_TEST_DATABASE_URL to a dedicated migrated test database")
class MedicineCatalogueTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine(TEST_DATABASE_URL)
        self.connection = await self.engine.connect()
        self.transaction = await self.connection.begin()
        self.session = AsyncSession(
            bind=self.connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )
        self.users = {}
        for code in ("patient", "admin", "super_admin"):
            role = await self.session.scalar(select(Role).where(Role.code == code))
            if role is None:
                role = Role(code=code, label=code)
                self.session.add(role)
                await self.session.flush()
            user = User(firebase_uid=str(uuid4()), display_name=code, status="active")
            self.session.add(user)
            await self.session.flush()
            self.session.add(UserRole(user_id=user.id, role_id=role.id))
            self.users[code] = user
        await self.session.flush()
        self.actor = self.users["patient"]

        async def session_override():
            yield self.session

        async def user_override():
            return self.actor

        app.dependency_overrides[get_session] = session_override
        app.dependency_overrides[get_current_user] = user_override
        self.client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()
        app.dependency_overrides.clear()
        await self.session.close()
        await self.transaction.rollback()
        await self.connection.close()
        await self.engine.dispose()

    async def test_search_sort_pagination_and_invalid_query(self):
        now = datetime.now(UTC)
        self.session.add_all([
            Medicine(generic_name="Older", is_sample=True, created_at=now - timedelta(days=1)),
            Medicine(generic_name="Newer", is_sample=True, created_at=now),
            Medicine(generic_name="Hidden", is_sample=False, created_at=now),
        ])
        await self.session.flush()
        response = await self.client.get("/api/v1/medicines", params={"page_size": 1})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["total"], 2)
        self.assertEqual(response.json()["items"][0]["generic_name"], "Newer")
        response = await self.client.get("/api/v1/medicines", params={"page_size": 1, "page": 2})
        self.assertEqual(response.json()["items"][0]["generic_name"], "Older")
        response = await self.client.get("/api/v1/medicines", params={"q": "old"})
        self.assertEqual(response.json()["total"], 1)
        response = await self.client.get("/api/v1/medicines", params={"unknown": "x"})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["code"], "VALIDATION_ERROR")
        self.assertIn("query.unknown", response.json()["field_errors"])

    async def test_write_permission_and_atomic_audit(self):
        payload = {"generic_name": "Test medicine"}
        for role in ("patient", "admin"):
            self.actor = self.users[role]
            response = await self.client.post("/api/v1/admin/medicines", json=payload)
            self.assertEqual(response.status_code, 403)
            self.assertEqual(response.json()["code"], "ACCESS_DENIED")
        self.actor = self.users["super_admin"]
        response = await self.client.post("/api/v1/admin/medicines", json=payload)
        self.assertEqual(response.status_code, 201, response.text)
        self.assertTrue(response.json()["is_sample"])
        event = await self.session.scalar(
            select(AuditEvent).where(AuditEvent.target_id == response.json()["id"])
        )
        self.assertIsNotNone(event)
        self.assertEqual(event.action, "medicine.created")
        self.assertEqual(event.actor_user_id, self.actor.id)
        response = await self.client.post("/api/v1/admin/medicines", json={"generic_name": " "})
        self.assertEqual(response.status_code, 422)
