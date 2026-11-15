"""PostgreSQL integration checks for append-only audit persistence."""

import os
import unittest
from uuid import uuid4

from sqlalchemy import delete, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import models as _models  # noqa: F401 - register ORM metadata
from app.modules.audit.models import AuditEvent
from app.modules.audit.service import append_event

TEST_DATABASE_URL = os.getenv("ML56_TEST_DATABASE_URL")


@unittest.skipUnless(
    TEST_DATABASE_URL,
    "Set ML56_TEST_DATABASE_URL to a dedicated database migrated to head",
)
class AuditAppendOnlyPostgreSQLTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine(TEST_DATABASE_URL)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def test_audit_event_cannot_be_updated_or_deleted(self):
        async with self.sessions() as session:
            event = await append_event(
                session,
                action="test.append_only",
                target_type="test_record",
                outcome="success",
                request_id=f"test-{uuid4()}",
            )
            event_id = event.id
            await session.commit()

        with self.assertRaises(DBAPIError):
            async with self.sessions() as session:
                await session.execute(
                    update(AuditEvent).where(AuditEvent.id == event_id).values(outcome="failed")
                )
                await session.commit()

        with self.assertRaises(DBAPIError):
            async with self.sessions() as session:
                await session.execute(delete(AuditEvent).where(AuditEvent.id == event_id))
                await session.commit()
