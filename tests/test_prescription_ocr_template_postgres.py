"""PostgreSQL integration checks for OCR and prescription template storage."""

import os
from datetime import date
from decimal import Decimal
from unittest import IsolatedAsyncioTestCase, skipUnless
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.database import models as _models  # noqa: F401
from app.modules.documents.models import Document
from app.modules.identity.models import User
from app.modules.prescriptions.core_models import Prescription
from app.modules.prescriptions.ocr_template_models import (
    PrescriptionOcrJobModel,
    PrescriptionTemplateItemModel,
    PrescriptionTemplateModel,
)
from app.modules.profiles.models import PatientProfile

TEST_DATABASE_URL = os.getenv("PRESCRIPTION_STORAGE_TEST_DATABASE_URL")


@skipUnless(
    TEST_DATABASE_URL, "Set PRESCRIPTION_STORAGE_TEST_DATABASE_URL to a migrated test database"
)
class PrescriptionStoragePostgresTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        engine = create_async_engine(TEST_DATABASE_URL)
        self.addAsyncCleanup(engine.dispose)
        connection = await engine.connect()
        self.addAsyncCleanup(connection.close)
        transaction = await connection.begin()
        self.addAsyncCleanup(transaction.rollback)
        self.db = AsyncSession(bind=connection, expire_on_commit=False)
        self.addAsyncCleanup(self.db.close)
        self.patient = User(
            firebase_uid=str(uuid4()), display_name="Synthetic patient", status="active"
        )
        self.doctor = User(
            firebase_uid=str(uuid4()), display_name="Synthetic doctor", status="active"
        )
        self.db.add_all([self.patient, self.doctor])
        await self.db.flush()
        self.db.add(
            PatientProfile(user_id=self.patient.id, date_of_birth=date(1990, 1, 1), sex="female")
        )
        await self.db.flush()

    async def test_ocr_retains_original_and_recovers_to_linked_draft(self):
        document = Document(
            owner_user_id=self.patient.id,
            patient_user_id=self.patient.id,
            uploaded_by=self.patient.id,
            source="patient",
            document_type="prescription",
            original_filename="synthetic.pdf",
            storage_key=str(uuid4()),
            media_type="application/pdf",
            byte_size=42,
        )
        self.db.add(document)
        await self.db.flush()
        job = PrescriptionOcrJobModel(
            patient_user_id=self.patient.id,
            original_document_id=document.id,
            requested_by_user_id=self.patient.id,
            status="failed",
            attempt_count=1,
            error_code="synthetic_error",
        )
        self.db.add(job)
        await self.db.flush()
        draft = Prescription(
            patient_user_id=self.patient.id,
            source="patient",
            created_by=self.patient.id,
            original_document_id=document.id,
        )
        self.db.add(draft)
        await self.db.flush()
        job.status = "draft_created"
        job.attempt_count = 2
        job.error_code = None
        job.resulting_prescription_id = draft.id
        await self.db.flush()
        job_id, document_id, draft_id = job.id, document.id, draft.id
        self.db.expire_all()
        stored = await self.db.get(PrescriptionOcrJobModel, job_id)
        self.assertEqual(stored.original_document_id, document_id)
        self.assertEqual(stored.resulting_prescription_id, draft_id)
        self.assertEqual(stored.attempt_count, 2)
        self.assertEqual(stored.status.value, "draft_created")

    async def test_template_owner_items_and_constraints(self):
        template = PrescriptionTemplateModel(
            doctor_user_id=self.doctor.id, name="Synthetic", template_type="template"
        )
        self.db.add(template)
        await self.db.flush()
        item = PrescriptionTemplateItemModel(
            template_id=template.id,
            position=1,
            medicine_name_snapshot="Synthetic medicine",
            dose_text="1 tablet",
            frequency="daily",
            quantity=Decimal("2"),
            quantity_unit="tablet",
        )
        self.db.add(item)
        await self.db.flush()
        result = await self.db.execute(
            select(PrescriptionTemplateModel, PrescriptionTemplateItemModel)
            .join(
                PrescriptionTemplateItemModel,
                PrescriptionTemplateItemModel.template_id == PrescriptionTemplateModel.id,
            )
            .where(PrescriptionTemplateModel.doctor_user_id == self.doctor.id)
        )
        saved_template, saved_item = result.one()
        self.assertEqual(saved_template.id, template.id)
        self.assertEqual(saved_item.id, item.id)
        self.assertEqual(saved_item.quantity, Decimal("2"))
        async with self.db.begin_nested():
            with self.assertRaises(IntegrityError):
                self.db.add(
                    PrescriptionTemplateModel(
                        doctor_user_id=self.doctor.id, name="synthetic", template_type="template"
                    )
                )
                await self.db.flush()
        async with self.db.begin_nested():
            with self.assertRaises(IntegrityError):
                self.db.add(
                    PrescriptionTemplateItemModel(
                        template_id=template.id,
                        position=2,
                        medicine_name_snapshot="Synthetic",
                        dose_text="1",
                        frequency="daily",
                        quantity=Decimal("0"),
                        quantity_unit="tablet",
                    )
                )
                await self.db.flush()
