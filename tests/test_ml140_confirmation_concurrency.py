"""Independent-session confirmation races against edits and draft deletion."""

import asyncio
import os
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, skipUnless
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.modules.audit.models import AuditEvent
from app.modules.identity.auth import AuthenticationError
from app.modules.prescriptions import service
from app.modules.prescriptions.core_models import Prescription
from app.modules.prescriptions.schemas import PrescriptionInput
from tests.test_ml140_confirmation_postgres import synthetic_draft, synthetic_patient

URL = os.getenv("ML140_CONCURRENCY_DATABASE_URL")


@skipUnless(URL, "Set ML140_CONCURRENCY_DATABASE_URL to a separate migrated disposable database")
class ConfirmationConcurrencyTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine(URL)
        self.addAsyncCleanup(self.engine.dispose)
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            self.patient = await synthetic_patient(db)
            rx = await synthetic_draft(db, self.patient.id)
            self.rx_id = rx.id
            await db.commit()

    async def action(self, kind):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            request = SimpleNamespace(state=SimpleNamespace(request_id=str(uuid4())))
            args = dict(session=db, user=self.patient, request=request, prescription_id=self.rx_id)
            try:
                if kind == "confirm":
                    detail = await service.confirm_patient_draft(**args)
                    return 200, detail.model_dump(mode="json")
                if kind == "edit":
                    payload = PrescriptionInput(
                        items=[
                            dict(
                                medicine_name_snapshot="Edited synthetic medicine",
                                dose_text="Edited dose",
                                frequency="daily",
                                is_prn=False,
                            )
                        ]
                    )
                    detail = await service.replace_prescription_draft(
                        **args, expected_version=1, payload=payload
                    )
                    return 200, detail.model_dump(mode="json")
                await service.delete_draft(**args)
                return 204, None
            except AuthenticationError as exc:
                return exc.status_code, exc.detail

    async def test_concurrent_confirmations_persist_one_transition_and_audit(self):
        results = await asyncio.wait_for(
            asyncio.gather(self.action("confirm"), self.action("confirm")), timeout=15
        )
        self.assertEqual([result[0] for result in results], [200, 200])
        self.assertEqual(results[0][1], results[1][1])
        async with AsyncSession(self.engine) as db:
            rx = await db.get(Prescription, self.rx_id)
            self.assertEqual(rx.version, 2)
            count = await db.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(
                    AuditEvent.target_id == self.rx_id,
                    AuditEvent.action == "prescription.patient.confirmed",
                )
            )
            self.assertEqual(count, 1)

    async def test_concurrent_confirmation_and_edit_never_rewrite_confirmed_items(self):
        confirm, edit = await asyncio.wait_for(
            asyncio.gather(self.action("confirm"), self.action("edit")), timeout=15
        )
        self.assertEqual(confirm[0], 200)
        self.assertIn(edit[0], [200, 409])
        async with AsyncSession(self.engine) as db:
            rx = await db.get(Prescription, self.rx_id)
            self.assertEqual(rx.status, "active")
            self.assertEqual(rx.source, "patient")
            self.assertEqual(rx.version, 3 if edit[0] == 200 else 2)
        # Whichever draft won the row lock is frozen after confirmation.
        self.assertEqual((await self.action("edit"))[0], 409)

    async def test_concurrent_confirmation_and_deletion_have_one_published_outcome(self):
        confirm, deletion = await asyncio.wait_for(
            asyncio.gather(self.action("confirm"), self.action("delete")), timeout=15
        )
        self.assertIn((confirm[0], deletion[0]), [(200, 409), (404, 204)])
        async with AsyncSession(self.engine) as db:
            rx = await db.get(Prescription, self.rx_id)
            count = await db.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(
                    AuditEvent.target_id == self.rx_id,
                    AuditEvent.action == "prescription.patient.confirmed",
                )
            )
            self.assertEqual(count, 1 if confirm[0] == 200 else 0)
            if confirm[0] == 200:
                self.assertEqual(rx.status, "active")
            else:
                self.assertIsNone(rx)
