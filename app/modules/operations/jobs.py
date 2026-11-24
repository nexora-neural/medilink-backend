"""Explicit retry allowlist. Never import or execute a path from a job row."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class RetryHandler:
    # Handler executes in the same transaction as the request and audit update.
    # External effects require a provider idempotency key/outbox before inclusion.
    run: Callable[..., Awaitable[None]]
    authorize: Callable[..., Awaitable[bool]]
    max_attempts: int = 3  # total attempts, including the original attempt


async def database_check(db: AsyncSession, job, actor_id):
    await db.execute(select(1))


async def operational_job_allowed(db: AsyncSession, job, actor_id):
    # Administrative authorization is checked independently by the worker.
    return True


HANDLERS = {
    "operations.database_check": RetryHandler(database_check, operational_job_allowed),
}
