"""Independent-session races between lifecycle writers and activation."""

import asyncio
import os
from datetime import UTC, datetime
from unittest import IsolatedAsyncioTestCase, skipUnless
from unittest.mock import patch
from uuid import UUID

from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.database.session import get_session
from app.main import app
from app.modules.identity.auth import get_current_user
from app.modules.identity.request_limits import require_authenticated_rate_limit
from app.modules.prescriptions.core_models import Prescription, PrescriptionItem
from app.modules.prescriptions.lifecycle_models import PrescriptionLifecycleEvent
from tests.test_ml140_confirmation_postgres import synthetic_draft, synthetic_patient

URL = os.getenv("ML154_CONCURRENCY_DATABASE_URL")


@skipUnless(URL, "Set ML154_CONCURRENCY_DATABASE_URL to a separate migrated disposable database")
class LifecycleConcurrencyTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine(URL)
        self.addAsyncCleanup(self.engine.dispose)
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            self.patient = await synthetic_patient(db)
            self.rx = await synthetic_draft(db, self.patient.id)
            self.rx.status, self.rx.issued_at = "active", datetime.now(UTC)
            await db.commit()

        async def session():
            async with AsyncSession(self.engine, expire_on_commit=False) as db:
                yield db

        overrides = patch.dict(
            app.dependency_overrides,
            {
                get_session: session,
                get_current_user: lambda: self.patient,
                require_authenticated_rate_limit: lambda: None,
            },
        )
        overrides.start()
        self.addCleanup(overrides.stop)
        self.client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        self.addAsyncCleanup(self.client.aclose)
        self.path = f"/api/v1/prescriptions/{self.rx.id}"
        self.body = dict(
            reason="Concurrent synthetic correction",
            items=[
                dict(
                    medicine_name_snapshot="Synthetic",
                    dose_text="Original dose",
                    frequency="daily",
                    is_prn=False,
                )
            ],
        )

    async def test_concurrent_corrections_produce_one_linked_draft(self):
        responses = await asyncio.wait_for(
            asyncio.gather(
                self.client.post(self.path + "/versions", json=self.body),
                self.client.post(self.path + "/renewals", json=self.body),
            ),
            timeout=20,
        )
        self.assertEqual(sorted(r.status_code for r in responses), [201, 409])
        async with AsyncSession(self.engine) as db:
            children = await db.scalar(
                select(func.count())
                .select_from(Prescription)
                .where(Prescription.previous_prescription_id == self.rx.id)
            )
            self.assertEqual(children, 1)

    async def test_concurrent_terminal_actions_commit_one_status_and_history(self):
        responses = await asyncio.wait_for(
            asyncio.gather(
                self.client.post(self.path + "/cancel", json={"reason": "Synthetic cancel"}),
                self.client.post(self.path + "/complete", json={"reason": "Synthetic complete"}),
            ),
            timeout=20,
        )
        self.assertEqual(sorted(r.status_code for r in responses), [201, 409])
        async with AsyncSession(self.engine) as db:
            self.assertEqual(
                await db.scalar(
                    select(func.count())
                    .select_from(PrescriptionLifecycleEvent)
                    .where(PrescriptionLifecycleEvent.prescription_id == self.rx.id)
                ),
                1,
            )
            rx = await db.get(Prescription, self.rx.id)
            self.assertEqual(rx.version, 2)

    async def test_confirmation_racing_parent_cancel_does_not_publish_two_outcomes(self):
        draft = await self.client.post(self.path + "/versions", json=self.body)
        self.assertEqual(draft.status_code, 201, draft.text)
        child_id = draft.json()["prescription"]["id"]
        confirm, cancel = await asyncio.wait_for(
            asyncio.gather(
                self.client.post(f"/api/v1/prescriptions/{child_id}/confirm"),
                self.client.post(self.path + "/cancel", json={"reason": "Synthetic stop"}),
            ),
            timeout=20,
        )
        self.assertIn((confirm.status_code, cancel.status_code), [(200, 409), (409, 201)])
        async with AsyncSession(self.engine) as db:
            old = await db.get(Prescription, self.rx.id)
            child = await db.get(Prescription, UUID(child_id))
            self.assertEqual(
                (old.status, child.status),
                ("superseded", "active") if confirm.status_code == 200 else ("cancelled", "draft"),
            )

    async def test_two_legacy_successors_cannot_both_activate(self):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            children = []
            for _ in range(2):
                rx = await synthetic_draft(db, self.patient.id)
                rx.previous_prescription_id = self.rx.id
                rx.status_reason = "Synthetic reissue"
                children.append(rx.id)
            await db.commit()
        responses = await asyncio.wait_for(
            asyncio.gather(
                *[self.client.post(f"/api/v1/prescriptions/{child}/confirm") for child in children]
            ),
            timeout=20,
        )
        self.assertEqual(sorted(r.status_code for r in responses), [200, 409])
        async with AsyncSession(self.engine) as db:
            active = await db.scalar(
                select(func.count())
                .select_from(Prescription)
                .where(Prescription.id.in_(children), Prescription.status == "active")
            )
            self.assertEqual(active, 1)
            old_item = await db.scalar(
                select(PrescriptionItem).where(PrescriptionItem.prescription_id == self.rx.id)
            )
            self.assertEqual(old_item.dose_text, "Original dose")
