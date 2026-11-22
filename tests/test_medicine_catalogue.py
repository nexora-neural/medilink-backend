"""Integration tests against a separately provisioned PostgreSQL test database."""

import os
from datetime import UTC, datetime, timedelta
from unittest import IsolatedAsyncioTestCase, skipUnless
from unittest.mock import patch
from uuid import uuid4

from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.database.session import get_session
from app.main import app
from app.modules.audit.models import AuditEvent
from app.modules.identity.auth import get_current_user, get_verified_identity
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

        async def verified_identity_override():
            await self.session.refresh(self.actor)
            return {"uid": self.actor.firebase_uid}

        counter_engine = patch("app.core.rate_limits.engine", self.engine)
        counter_engine.start()
        self.addCleanup(counter_engine.stop)
        app.dependency_overrides[get_verified_identity] = verified_identity_override
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
        response = await self.client.get(
            "/api/v1/medicines", params={"synthetic-query-secret": "x"}
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["code"], "VALIDATION_ERROR")
        self.assertIn("query", response.json()["field_errors"])
        self.assertNotIn("synthetic-query-secret", response.text)

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

    async def test_management_list_returns_saved_entry_and_denies_regular_admin(self):
        path = "/api/v1/admin/medicines"
        payload = {
            "generic_name": "Catalogue test medicine",
            "brand_name": "Catalogue test brand",
            "strength": "500 mg",
            "form": "Tablet",
        }

        self.actor = self.users["admin"]
        denied = await self.client.get(path)
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(denied.json()["code"], "ACCESS_DENIED")

        self.actor = self.users["super_admin"]
        created = await self.client.post(path, json=payload)
        self.assertEqual(created.status_code, 201, created.text)

        listed = await self.client.get(path)
        self.assertEqual(listed.status_code, 200, listed.text)

        saved_id = created.json()["id"]
        saved = next(
            (row for row in listed.json()["items"] if row["id"] == saved_id),
            None,
        )
        self.assertIsNotNone(saved)
        self.assertTrue(saved["is_sample"])
        self.assertEqual({field: saved[field] for field in payload}, payload)

    async def test_duplicate_create_returns_conflict_and_search_finds_one_medicine(self):
        path = "/api/v1/admin/medicines"
        payload = {
            "generic_name": "QA Paracetamol",
            "brand_name": "QA Relief",
            "strength": "500 mg",
            "form": "tablet",
        }

        self.actor = self.users["super_admin"]
        created = await self.client.post(path, json=payload)
        self.assertEqual(created.status_code, 201, created.text)
        medicine_id = created.json()["id"]

        case_variant = {field: f" {value.upper()} " for field, value in payload.items()}
        for duplicate in (payload, case_variant):
            response = await self.client.post(path, json=duplicate)
            self.assertEqual(response.status_code, 409, response.text)
            self.assertEqual(response.json()["code"], "MEDICINE_ALREADY_EXISTS")
            self.assertEqual(response.json()["request_id"], response.headers["X-Request-ID"])

        self.actor = self.users["patient"]
        for query in payload.values():
            response = await self.client.get("/api/v1/medicines", params={"q": query})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["total"], 1)
            self.assertEqual([item["id"] for item in response.json()["items"]], [medicine_id])

        audit_count = await self.session.scalar(
            select(func.count()).select_from(AuditEvent).where(
                AuditEvent.target_id == medicine_id,
                AuditEvent.action == "medicine.created",
            )
        )
        self.assertEqual(audit_count, 1)

        self.actor = self.users["super_admin"]
        for field, value in {
            "generic_name": "QA Ibuprofen",
            "brand_name": "QA Relief Extra",
            "strength": "250 mg",
            "form": "capsule",
        }.items():
            with self.subTest(field=field):
                response = await self.client.post(path, json={**payload, field: value})
                self.assertEqual(response.status_code, 201, response.text)

    async def test_duplicate_create_matches_missing_optional_fields_in_sample_catalogue(self):
        path = "/api/v1/admin/medicines"
        self.session.add(Medicine(generic_name="QA Existing", is_sample=False))
        await self.session.flush()
        self.actor = self.users["super_admin"]

        created = await self.client.post(path, json={"generic_name": "QA Existing"})
        self.assertEqual(created.status_code, 201, created.text)
        duplicate = await self.client.post(
            path,
            json={
                "generic_name": "QA Existing",
                "brand_name": None,
                "strength": None,
                "form": None,
            },
        )
        self.assertEqual(duplicate.status_code, 409, duplicate.text)
