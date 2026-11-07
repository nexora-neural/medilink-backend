"""Shared rate limit for authenticated profile requests."""

from contextlib import asynccontextmanager
from hashlib import sha256
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Request
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.config import settings
from app.core.rate_limits import enforce_rate_limit
from app.modules.identity.auth import get_verified_identity


@asynccontextmanager
async def profile_rate_limit_lifespan(app: FastAPI):
    """Reuse one Redis client per worker and close it at shutdown."""
    client = Redis.from_url(
        settings.redis_url,
        socket_connect_timeout=2,
        socket_timeout=2,
    )
    app.state.profile_rate_limit_client = client
    try:
        yield
    finally:
        await client.aclose()


async def get_profile_rate_limit_client(request: Request) -> Redis:
    """Return the Redis client managed by the profile router lifespan."""
    return request.app.state.profile_rate_limit_client


async def require_profile_rate_limit(
    identity: Annotated[dict[str, Any], Depends(get_verified_identity)],
    client: Annotated[Redis, Depends(get_profile_rate_limit_client)],
) -> None:
    """Apply one shared quota across the authenticated profile endpoints."""
    account_key = sha256(identity["uid"].encode("utf-8")).hexdigest()
    try:
        await enforce_rate_limit(
            client,
            key=f"medilink:{settings.app_env}:rate-limit:authenticated:{account_key}",
            limit=settings.rate_limit_authenticated_per_minute,
            window_seconds=60,
        )
    except RedisError:
        raise HTTPException(
            status_code=503,
            detail="Request rate limiting is temporarily unavailable.",
        ) from None