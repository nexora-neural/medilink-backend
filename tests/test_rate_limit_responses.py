from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock, patch

from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis

from app.core.rate_limits import enforce_rate_limit
from app.main import app


class RateLimitResponseTests(IsolatedAsyncioTestCase):
    async def test_exceeded_limit_returns_429_with_retry_after(self):
        redis_client = Mock(spec=Redis)
        redis_client.eval = AsyncMock(side_effect=[0, 1001])
        endpoint_calls = []

        with patch.object(app.router, "routes", app.router.routes.copy()):

            @app.get("/_test/rate-limit")
            async def limited_endpoint():
                await enforce_rate_limit(
                    redis_client,
                    key="medilink:test:authenticated:account-1",
                    limit=120,
                    window_seconds=60,
                )
                endpoint_calls.append("allowed")
                return {"status": "ok"}

            transport = ASGITransport(app=app, raise_app_exceptions=True)
            async with AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as client:
                allowed = await client.get("/_test/rate-limit")
                blocked = await client.get("/_test/rate-limit")

        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(blocked.status_code, 429)
        self.assertEqual(blocked.headers["Retry-After"], "2")
        self.assertEqual(endpoint_calls, ["allowed"])
        self.assertEqual(
            blocked.json(),
            {
                "code": "RATE_LIMITED",
                "message": "Too many requests. Please try again later.",
                "field_errors": {},
                "request_id": blocked.headers["X-Request-ID"],
            },
        )