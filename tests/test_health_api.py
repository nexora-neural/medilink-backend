from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

from httpx import ASGITransport, AsyncClient

from app.main import app
from tests.fixtures import synthetic_database_check


class HealthApiTests(IsolatedAsyncioTestCase):
    async def request(self, path: str):
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.get(path)

    async def test_api_health_returns_success(self):
        response = await self.request("/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    async def test_database_health_uses_synthetic_success_fixture(self):
        synthetic_check = synthetic_database_check()
        with patch("app.api.router.check_database_connection", synthetic_check):
            response = await self.request("/api/v1/health/database")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"database": "ok"})
        synthetic_check.assert_awaited_once_with()

    async def test_database_health_failure_returns_server_error(self):
        synthetic_check = synthetic_database_check(fail=True)
        with patch("app.api.router.check_database_connection", synthetic_check):
            response = await self.request("/api/v1/health/database")

        self.assertEqual(response.status_code, 500)
        synthetic_check.assert_awaited_once_with()
