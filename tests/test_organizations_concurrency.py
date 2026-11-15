"""PostgreSQL integration test for concurrent organization membership requests."""

import asyncio
import os
from datetime import UTC, datetime, timedelta
from unittest import IsolatedAsyncioTestCase, skipUnless
from unittest.mock import patch
from uuid import uuid4

from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database.session import get_session
from app.main import app
from app.modules.identity.auth import get_current_user
from app.modules.identity.models import Role, User, UserRole
from app.modules.organizations import repository
from app.modules.organizations.models import Organization, OrganizationMembership
from app.modules.profiles.models import ProfessionalProfile

TEST_DATABASE_URL = os.getenv("ML37_TEST_DATABASE_URL")


@skipUnless(TEST_DATABASE_URL, "Set ML37_TEST_DATABASE_URL to a dedicated migrated test database")
class OrganizationMembershipConcurrencyTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine(TEST_DATABASE_URL)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.user_id = uuid4()
        self.organization_id = uuid4()
        self.firebase_uid = f"ml37-concurrency-{uuid4()}"
        async with self.sessions() as session:
            role = await session.scalar(select(Role).where(Role.code == "doctor"))
            if role is None:
                role = Role(code="doctor", label="Doctor")
                session.add(role)
                await session.flush()
            self.role_id = role.id
            self.actor = User(
                id=self.user_id,
                firebase_uid=self.firebase_uid,
                display_name="ML-37 concurrency test",
                status="active",
            )
            organization = Organization(
                id=self.organization_id,
                name=f"ML-37 concurrency test {self.organization_id}",
                type="clinic",
                status="active",
            )
            session.add_all(
                [
                    self.actor,
                    organization,
                    UserRole(user_id=self.user_id, role_id=self.role_id),
                    ProfessionalProfile(
                        user_id=self.user_id,
                        role="doctor",
                        doctor_reference=f"ML37-{self.user_id}",
                    ),
                ]
            )
            await session.commit()

        async def session_override():
            async with self.sessions() as session:
                yield session

        async def actor_override():
            return self.actor

        app.dependency_overrides[get_session] = session_override
        app.dependency_overrides[get_current_user] = actor_override
        self.client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()
        app.dependency_overrides.clear()
        # The test database is disposable. Keep generated audit events because
        # production audit records are append-only and must not be deleted.
        await self.engine.dispose()

    async def test_concurrent_duplicate_requests_return_one_created_and_one_conflict(self):
        original_lookup = repository.get_existing_membership
        both_lookups_finished = asyncio.Event()
        lookup_count = 0

        async def synchronized_lookup(*args, **kwargs):
            nonlocal lookup_count
            membership = await original_lookup(*args, **kwargs)
            lookup_count += 1
            if lookup_count == 2:
                both_lookups_finished.set()
            await both_lookups_finished.wait()
            return membership

        payload = {"organization_id": str(self.organization_id), "role": "doctor"}
        with patch.object(repository, "get_existing_membership", synchronized_lookup):
            responses = await asyncio.gather(
                self.client.post("/api/v1/me/memberships", json=payload),
                self.client.post("/api/v1/me/memberships", json=payload),
            )

        self.assertEqual(
            sorted(response.status_code for response in responses),
            [201, 409],
            [response.text for response in responses],
        )
        conflict = next(response for response in responses if response.status_code == 409)
        self.assertEqual(conflict.json()["code"], "INVALID_STATE")
        async with self.sessions() as session:
            count = await session.scalar(
                select(func.count())
                .select_from(OrganizationMembership)
                .where(
                    OrganizationMembership.organization_id == self.organization_id,
                    OrganizationMembership.user_id == self.user_id,
                    OrganizationMembership.role == "doctor",
                )
            )
        self.assertEqual(count, 1)

    async def test_public_organization_list_uses_creation_order_and_stable_pagination(self):
        prefix = f"ML252 ordering {uuid4()}"
        now = datetime.now(UTC)
        tie_low_id, tie_high_id = sorted((uuid4(), uuid4()))
        older = Organization(
            name=f"{prefix} A older",
            type="clinic",
            status="active",
            created_at=now - timedelta(days=2),
        )
        tie_low = Organization(
            id=tie_low_id,
            name=f"{prefix} M tie low",
            type="clinic",
            status="active",
            created_at=now - timedelta(days=1),
        )
        tie_high = Organization(
            id=tie_high_id,
            name=f"{prefix} M tie high",
            type="clinic",
            status="active",
            created_at=now - timedelta(days=1),
        )
        newer = Organization(
            name=f"{prefix} Z newer",
            type="clinic",
            status="active",
            created_at=now,
        )
        async with self.sessions() as session:
            session.add_all([older, tie_low, tie_high, newer])
            await session.commit()

        first_page = await self.client.get(
            "/api/v1/organizations", params={"q": prefix, "page": 1, "page_size": 2}
        )
        second_page = await self.client.get(
            "/api/v1/organizations", params={"q": prefix, "page": 2, "page_size": 2}
        )

        self.assertEqual(first_page.status_code, 200, first_page.text)
        self.assertEqual(second_page.status_code, 200, second_page.text)
        self.assertEqual(first_page.json()["total"], 4)
        self.assertEqual(second_page.json()["total"], 4)
        actual_ids = [
            item["id"]
            for response in (first_page, second_page)
            for item in response.json()["items"]
        ]
        expected_ids = [str(newer.id), str(tie_high.id), str(tie_low.id), str(older.id)]
        self.assertEqual(actual_ids, expected_ids)
        self.assertEqual(len(actual_ids), len(set(actual_ids)))
