"""Shared fixed-window counters for backend request limits."""

import logging
from asyncio import timeout
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from math import ceil

from fastapi import Request
from fastapi.responses import JSONResponse
from sqlalchemy import CheckConstraint, Column, DateTime, Integer, String, Table, case, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import settings
from app.database.base import Base
from app.database.session import engine

logger = logging.getLogger(__name__)

request_rate_limits = Table(
    "request_rate_limits",
    Base.metadata,
    Column("bucket_key", String(64), primary_key=True),
    Column("window_end", DateTime(timezone=True), nullable=False, index=True),
    Column("request_count", Integer, nullable=False),
    CheckConstraint("request_count > 0", name="positive_count"),
)


@dataclass(frozen=True)
class Quota:
    scope: str
    actor: str
    limit: int


class RateLimitExceeded(Exception):
    """The request exceeded a configured rate limit."""

    def __init__(self, retry_after: int) -> None:
        super().__init__("Request rate limit exceeded.")
        self.retry_after = retry_after


class RateLimitUnavailable(Exception):
    """The shared counter store is temporarily unavailable."""


async def enforce_rate_limit(request: Request, *quotas: Quota, window_seconds: int) -> None:
    """Charge all quotas atomically before running the operation."""
    try:
        async with timeout(3), engine.begin() as connection:
            now = await connection.scalar(select(func.clock_timestamp()))
            end_timestamp = (int(now.timestamp()) // window_seconds + 1) * window_seconds
            window_end = datetime.fromtimestamp(end_timestamp, tz=now.tzinfo)
            counters = request_rate_limits.c
            for quota in quotas:
                key = sha256(
                    f"{settings.app_env}:{quota.scope}:{quota.actor}".encode()
                ).hexdigest()
                new = insert(request_rate_limits).values(
                    bucket_key=key, window_end=window_end, request_count=1
                )
                updated = new.on_conflict_do_update(
                    index_elements=[counters.bucket_key],
                    set_={
                        "window_end": window_end,
                        "request_count": case(
                            (counters.window_end < window_end, 1),
                            else_=counters.request_count + 1,
                        ),
                    },
                    where=(counters.window_end < window_end)
                    | (
                        (counters.window_end == window_end)
                        & (counters.request_count < quota.limit)
                    ),
                ).returning(counters.request_count)
                if await connection.scalar(updated) is None:
                    rejected_at = await connection.scalar(select(func.clock_timestamp()))
                    retry_after = max(1, ceil((window_end - rejected_at).total_seconds()))
                    raise RateLimitExceeded(retry_after)
    except (SQLAlchemyError, OSError, TimeoutError):
        logger.warning(
            "Rate-limit store unavailable (request_id=%s)",
            getattr(request.state, "request_id", ""),
        )
        raise RateLimitUnavailable from None


async def rate_limit_exceeded_handler(
    request: Request, exc: RateLimitExceeded
) -> JSONResponse:
    return JSONResponse(
        status_code=429,
        content={
            "code": "RATE_LIMITED",
            "message": "Too many requests. Please try again later.",
            "field_errors": {},
            "request_id": request.state.request_id,
        },
        headers={"Retry-After": str(exc.retry_after), "Cache-Control": "no-store"},
    )


async def rate_limit_unavailable_handler(
    request: Request, exc: RateLimitUnavailable
) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={
            "code": "SERVICE_UNAVAILABLE",
            "message": "Request rate limiting is temporarily unavailable.",
            "field_errors": {},
            "request_id": request.state.request_id,
        },
        headers={"Cache-Control": "no-store"},
    )
