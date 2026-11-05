"""Atomic Redis counters for backend request limits."""

from redis.asyncio import Redis

_RATE_LIMIT_SCRIPT = """
local key = KEYS[1]
local limit = tonumber(ARGV[1])
local window_ms = tonumber(ARGV[2])

local count = tonumber(redis.call("GET", key) or "0")
local remaining_ms = redis.call("PTTL", key)

if remaining_ms <= 0 then
    redis.call("SET", key, 1, "PX", window_ms)
    return 0
end

if count >= limit then
    return remaining_ms
end

redis.call("INCR", key)
return 0
"""


async def consume_rate_limit(
    client: Redis,
    *,
    key: str,
    limit: int,
    window_seconds: int,
) -> int:
    """Return zero when allowed, otherwise the retry delay in seconds.

    A fixed window begins with the first accepted request.
    Rejected requests do not extend the window.
    Redis failures propagate so the caller can return a safe service error.
    """
    if limit <= 0 or window_seconds <= 0:
        raise ValueError("Rate limit and window must be positive.")

    remaining_ms = int(
        await client.eval(
            _RATE_LIMIT_SCRIPT,
            1,
            key,
            limit,
            window_seconds * 1000,
        )
    )
    return (remaining_ms + 999) // 1000