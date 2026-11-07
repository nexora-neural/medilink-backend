from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock

from redis.asyncio import Redis
from redis.exceptions import ConnectionError

from app.core.rate_limits import consume_rate_limit


class RateLimitTests(IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = Mock(spec=Redis)
        self.client.eval = AsyncMock()

    async def consume(self) -> int:
        return await consume_rate_limit(
            self.client,
            key="medilink:test:authenticated:account-1",
            limit=120,
            window_seconds=60,
        )

    async def test_allowed_request_has_no_retry_delay(self):
        self.client.eval.return_value = 0

        self.assertEqual(await self.consume(), 0)
        self.client.eval.assert_awaited_once()

    async def test_blocked_request_rounds_retry_delay_up(self):
        for remaining_ms, expected_seconds in ((1, 1), (1000, 1), (1001, 2)):
            with self.subTest(remaining_ms=remaining_ms):
                self.client.eval.return_value = remaining_ms

                self.assertEqual(await self.consume(), expected_seconds)

    async def test_redis_connection_error_reaches_caller(self):
        self.client.eval.side_effect = ConnectionError("Redis unavailable")

        with self.assertRaises(ConnectionError):
            await self.consume()