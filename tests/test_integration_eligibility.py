"""HTTP contract and authorization coverage without a database dependency."""

from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.database.session import get_session
from app.modules.identity.auth import get_current_user
from app.modules.identity.request_limits import require_authenticated_rate_limit
from app.modules.integrations.router import router

PATH = "/api/v1/admin/integration-clients/eligible-laboratories"


class EligibleLaboratoryHttpTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.session = AsyncMock()
        self.session.scalar.return_value = uuid4()
        self.actor = SimpleNamespace(id=uuid4(), status="active")
        self.app = FastAPI()
        self.app.include_router(router, prefix="/api/v1")

        async def session():
            yield self.session

        async def actor():
            return self.actor

        async def rate_limit():
            pass

        self.app.dependency_overrides.update({
            get_session: session,
            get_current_user: actor,
            require_authenticated_rate_limit: rate_limit,
        })
        self.client = AsyncClient(transport=ASGITransport(app=self.app), base_url="http://test")
        self.addAsyncCleanup(self.client.aclose)

    async def test_paginated_options_are_minimal_and_never_cached(self):
        lab_id = uuid4()
        row = SimpleNamespace(id=lab_id, name="Eligible Lab", staff_email="PRIVATE")
        with patch(
            "app.modules.integrations.repository.list_approved_laboratories",
            new=AsyncMock(return_value=([row], 3)),
        ) as query:
            response = await self.client.get(PATH, params={"page": 2, "page_size": 1})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "items": [{"id": str(lab_id), "name": "Eligible Lab"}],
            "page": 2, "page_size": 1, "total": 3,
        })
        self.assertEqual(response.headers["cache-control"], "no-store")
        query.assert_awaited_once_with(self.session, page=2, page_size=1)

    async def test_delegated_admin_or_revoked_role_cannot_list_options(self):
        self.session.scalar.return_value = None
        with patch(
            "app.modules.integrations.repository.list_approved_laboratories",
            new_callable=AsyncMock,
        ) as query:
            response = await self.client.get(PATH)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "ACCESS_DENIED")
        query.assert_not_awaited()

    async def test_inactive_actor_cannot_list_options(self):
        self.actor.status = "suspended"
        response = await self.client.get(PATH)
        self.assertEqual(response.status_code, 403)

    async def test_missing_bearer_identity_is_rejected(self):
        del self.app.dependency_overrides[get_current_user]
        response = await self.client.get(PATH)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "AUTHENTICATION_REQUIRED")

    async def test_query_bounds_and_unknown_fields_are_rejected(self):
        for query in ("page=0", "page_size=0", "page_size=101", "role=lab_staff"):
            with self.subTest(query=query):
                response = await self.client.get(f"{PATH}?{query}")
                self.assertEqual(response.status_code, 422)

    async def test_empty_eligible_result_does_not_return_staff_data(self):
        with patch(
            "app.modules.integrations.repository.list_approved_laboratories",
            new=AsyncMock(return_value=([], 0)),
        ):
            response = await self.client.get(PATH)
        self.assertEqual(response.json(), {"items": [], "page": 1, "page_size": 20, "total": 0})
