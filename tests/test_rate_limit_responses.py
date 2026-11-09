from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from starlette.requests import Request

from app.core.config import settings
from app.core.rate_limits import Quota, RateLimitUnavailable
from app.modules.identity.request_limits import (
    require_resolution_rate_limit,
    require_upload_rate_limit,
)
from app.modules.integrations.request_limits import require_integration_rate_limit


class SpecializedRateLimitTests(IsolatedAsyncioTestCase):
    def setUp(self):
        self.request = Request({
            "type": "http",
            "client": ("::ffff:192.0.2.1", 1024),
            "headers": [(b"x-forwarded-for", b"198.51.100.1")],
        })
        self.identity = {"uid": "synthetic-account"}

    async def test_resolution_uses_verified_account_and_asgi_peer_ip(self):
        limiter = AsyncMock()
        with patch("app.modules.identity.request_limits.enforce_rate_limit", limiter):
            await require_resolution_rate_limit(self.request, self.identity)

        limiter.assert_awaited_once_with(
            self.request,
            Quota(
                "resolution-account",
                "synthetic-account",
                settings.rate_limit_resolution_per_account_per_minute,
            ),
            Quota("resolution-ip", "192.0.2.1", settings.rate_limit_resolution_per_ip_per_minute),
            window_seconds=60,
        )

    async def test_resolution_fails_closed_without_a_peer_ip(self):
        request = Request({"type": "http"})
        with self.assertRaises(RateLimitUnavailable):
            await require_resolution_rate_limit(request, self.identity)

    async def test_upload_uses_the_hourly_account_quota(self):
        limiter = AsyncMock()
        with patch("app.modules.identity.request_limits.enforce_rate_limit", limiter):
            await require_upload_rate_limit(self.request, self.identity)

        limiter.assert_awaited_once_with(
            self.request,
            Quota("upload", "synthetic-account", settings.rate_limit_uploads_per_account_per_hour),
            window_seconds=3600,
        )

    async def test_integration_uses_authenticated_client_id(self):
        client_id = uuid4()
        limiter = AsyncMock()
        with patch("app.modules.integrations.request_limits.enforce_rate_limit", limiter):
            await require_integration_rate_limit(self.request, client_id)

        limiter.assert_awaited_once_with(
            self.request,
            Quota(
                "integration", str(client_id), settings.rate_limit_integration_per_client_per_minute
            ),
            window_seconds=60,
        )
