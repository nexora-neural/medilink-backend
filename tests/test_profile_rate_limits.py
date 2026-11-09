from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import OperationalError

from app.core.config import settings
from app.core.rate_limits import Quota, RateLimitExceeded
from app.main import app
from app.modules.identity.auth import get_current_user, get_verified_identity


class AuthenticatedRateLimitTests(IsolatedAsyncioTestCase):
    def setUp(self):
        self.account_checks = 0

        async def verified_identity():
            return {"uid": "synthetic-account"}

        async def reject_local_account():
            self.account_checks += 1
            raise HTTPException(status_code=403, detail="Test account is unavailable.")

        overrides = patch.dict(
            app.dependency_overrides,
            {
                get_verified_identity: verified_identity,
                get_current_user: reject_local_account,
            },
        )
        overrides.start()
        self.addCleanup(overrides.stop)

    async def test_profile_and_medicine_routes_share_one_account_quota(self):
        limiter = AsyncMock(side_effect=[None, RateLimitExceeded(2)])
        with patch("app.modules.identity.request_limits.enforce_rate_limit", limiter):
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://testserver",
            ) as client:
                allowed_by_limiter = await client.get("/api/v1/me")
                blocked = await client.get(
                    "/api/v1/medicines", headers={"Origin": "http://localhost:4200"}
                )

        self.assertEqual(allowed_by_limiter.status_code, 403)
        self.assertEqual(self.account_checks, 1)
        self.assertEqual(blocked.status_code, 429)
        self.assertEqual(blocked.json()["code"], "RATE_LIMITED")
        self.assertEqual(blocked.headers["Retry-After"], "2")
        self.assertEqual(blocked.headers["Cache-Control"], "no-store")
        self.assertIn("Retry-After", blocked.headers["Access-Control-Expose-Headers"])
        self.assertEqual(blocked.json()["request_id"], blocked.headers["X-Request-ID"])

        for call in limiter.await_args_list:
            self.assertEqual(
                call.args[1],
                Quota(
                    "authenticated",
                    "synthetic-account",
                    settings.rate_limit_authenticated_per_minute,
                ),
            )
            self.assertEqual(call.kwargs["window_seconds"], 60)

    async def test_database_failure_returns_safe_503(self):
        failure = OperationalError("synthetic-sql-secret", None, OSError("synthetic-db-secret"))
        with (
            patch("app.core.rate_limits.engine", Mock(begin=Mock(side_effect=failure))),
            self.assertLogs("app.core.rate_limits", level="WARNING") as logs,
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://testserver"
            ) as client:
                response = await client.get("/api/v1/me")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["code"], "SERVICE_UNAVAILABLE")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.json()["request_id"], response.headers["X-Request-ID"])
        self.assertIn(response.headers["X-Request-ID"], logs.output[0])
        self.assertNotIn("secret", response.text + logs.output[0])
        self.assertEqual(self.account_checks, 0)
