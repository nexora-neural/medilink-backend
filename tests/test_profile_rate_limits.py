from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from redis.exceptions import ConnectionError

from app.core.config import settings
from app.main import app
from app.modules.identity.auth import get_current_user, get_verified_identity
from app.modules.profiles.rate_limit import get_profile_rate_limit_client


class ProfileRateLimitTests(IsolatedAsyncioTestCase):
    def setUp(self):
        self.redis_client = Mock(spec=Redis)
        self.redis_client.eval = AsyncMock()
        self.account_checks = 0

        async def verified_identity():
            return {"uid": "synthetic-profile-account"}

        async def redis_client():
            return self.redis_client

        async def reject_local_account():
            self.account_checks += 1
            raise HTTPException(status_code=403, detail="Test account is unavailable.")

        overrides = patch.dict(
            app.dependency_overrides,
            {
                get_verified_identity: verified_identity,
                get_profile_rate_limit_client: redis_client,
                get_current_user: reject_local_account,
            },
        )
        overrides.start()
        self.addCleanup(overrides.stop)

    async def test_profile_routes_share_one_account_counter(self):
        self.redis_client.eval.side_effect = [0, 1001]
        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=True),
            base_url="http://testserver",
        ) as client:
            allowed_by_limiter = await client.get("/api/v1/me")
            blocked = await client.patch("/api/v1/me", json={"phone": None})

        self.assertEqual(allowed_by_limiter.status_code, 403)
        self.assertEqual(self.account_checks, 1)
        self.assertEqual(blocked.status_code, 429)
        self.assertEqual(blocked.json()["code"], "RATE_LIMITED")
        self.assertEqual(blocked.headers["Retry-After"], "2")
        self.assertEqual(blocked.headers["Cache-Control"], "no-store")
        self.assertEqual(
            blocked.json()["request_id"],
            blocked.headers["X-Request-ID"],
        )

        first, second = self.redis_client.eval.await_args_list
        self.assertEqual(first.args[2], second.args[2])
        self.assertEqual(first.args[3], settings.rate_limit_authenticated_per_minute)
        self.assertEqual(first.args[4], 60_000)

    async def test_redis_failure_returns_safe_503(self):
        self.redis_client.eval.side_effect = ConnectionError("synthetic-redis-secret")
        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=True),
            base_url="http://testserver",
        ) as client:
            response = await client.get("/api/v1/me")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["code"], "SERVICE_UNAVAILABLE")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertNotIn("synthetic-redis-secret", response.text)
        self.assertEqual(self.account_checks, 0)