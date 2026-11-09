"""Rate-limit tests against a dedicated, migrated PostgreSQL database."""

import os
from asyncio import gather
from hashlib import sha256
from unittest import IsolatedAsyncioTestCase, skipUnless
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import func, text, update
from sqlalchemy.ext.asyncio import create_async_engine
from starlette.requests import Request

from app.core.config import settings
from app.core.rate_limits import Quota, RateLimitExceeded, enforce_rate_limit, request_rate_limits

TEST_DATABASE_URL = os.getenv("ML30_TEST_DATABASE_URL")


@skipUnless(TEST_DATABASE_URL, "Set ML30_TEST_DATABASE_URL to a dedicated migrated test database")
class PostgreSQLRateLimitTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine(TEST_DATABASE_URL)
        self.other_worker = create_async_engine(TEST_DATABASE_URL)
        self.addAsyncCleanup(self.engine.dispose)
        self.addAsyncCleanup(self.other_worker.dispose)
        self.request = Request({"type": "http"})

    async def charge(self, *quotas: Quota, engine=None) -> bool:
        with patch("app.core.rate_limits.engine", engine or self.engine):
            try:
                await enforce_rate_limit(self.request, *quotas, window_seconds=3600)
            except RateLimitExceeded as error:
                self.assertGreaterEqual(error.retry_after, 1)
                self.assertLessEqual(error.retry_after, 3600)
                return False
        return True

    async def test_concurrent_connections_share_limit_with_an_independent_pool(self):
        quota = Quota("test-concurrent", str(uuid4()), 8)
        self.assertTrue(await self.charge(quota, engine=self.other_worker))
        with patch("app.core.rate_limits.engine", self.engine):
            results = await gather(
                *(enforce_rate_limit(self.request, quota, window_seconds=3600) for _ in range(19)),
                return_exceptions=True,
            )
        self.assertEqual(results.count(None), 7, results)
        self.assertEqual(sum(isinstance(result, RateLimitExceeded) for result in results), 12)
        self.assertFalse(await self.charge(quota, engine=self.other_worker))

    async def test_rejected_ip_does_not_spend_account_quota(self):
        ip = Quota("test-ip", str(uuid4()), 1)
        another_ip = Quota("test-ip", str(uuid4()), 1)
        account = Quota("test-account", str(uuid4()), 1)
        other_account = Quota("test-account", str(uuid4()), 1)
        self.assertTrue(await self.charge(other_account, ip))
        self.assertFalse(await self.charge(account, ip))
        self.assertTrue(await self.charge(account, another_ip))
        self.assertFalse(await self.charge(account))

    async def test_expired_counter_resets_on_next_request(self):
        quota = Quota("test-expiry", str(uuid4()), 1)
        self.assertTrue(await self.charge(quota))
        self.assertFalse(await self.charge(quota))
        async with self.engine.begin() as connection:
            # Locate this test's row using the same namespace as the limiter.
            key = sha256(f"{settings.app_env}:{quota.scope}:{quota.actor}".encode()).hexdigest()
            await connection.execute(
                update(request_rate_limits)
                .where(request_rate_limits.c.bucket_key == key)
                .values(window_end=func.clock_timestamp() - text("INTERVAL '1 hour'"))
            )
        self.assertTrue(await self.charge(quota, engine=self.other_worker))
        self.assertFalse(await self.charge(quota))
