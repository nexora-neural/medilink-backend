"""Schema-contract checks for prescription OCR and template storage models."""

from unittest import TestCase

from sqlalchemy.dialects.postgresql import JSONB

from app.database import models as _models  # noqa: F401
from app.database.base import Base
from app.modules.prescriptions.enums import (
    PrescriptionOcrJobStatus,
    PrescriptionTemplateStatus,
    PrescriptionTemplateType,
)


class PrescriptionOcrTemplateStorageModelTests(TestCase):
    def test_ocr_job_retains_patient_document_and_draft_links(self):
        table = Base.metadata.tables["prescription_ocr_jobs"]
        foreign_keys = {fk.target_fullname for fk in table.foreign_keys}

        self.assertTrue(
            {
                "patient_profiles.user_id",
                "documents.id",
                "users.id",
                "prescriptions.id",
            }.issubset(foreign_keys)
        )
        self.assertIsInstance(table.c.extracted_fields.type, JSONB)
        self.assertEqual(
            [status.value for status in PrescriptionOcrJobStatus],
            ["queued", "processing", "ready_for_review", "failed", "draft_created"],
        )
        self.assertIn("uq_prescription_ocr_patient_document", {
            constraint.name for constraint in table.constraints
        })

    def test_template_is_owned_and_archived_by_status(self):
        table = Base.metadata.tables["prescription_templates"]

        self.assertIn("users.id", {fk.target_fullname for fk in table.foreign_keys})
        self.assertEqual(
            [value.value for value in PrescriptionTemplateType], ["favourite", "template"]
        )
        self.assertEqual(
            [value.value for value in PrescriptionTemplateStatus], ["active", "archived"]
        )
        self.assertIn(
            "uq_prescription_template_active_name", {index.name for index in table.indexes}
        )

    def test_template_items_keep_order_and_quantity_invariants(self):
        table = Base.metadata.tables["prescription_template_items"]
        foreign_keys = {fk.target_fullname for fk in table.foreign_keys}
        checks = {constraint.name for constraint in table.constraints}
        uniques = {constraint.name for constraint in table.constraints}

        self.assertEqual(foreign_keys, {"prescription_templates.id", "medicines.id"})
        self.assertTrue(
            {
                "ck_prescription_template_item_position",
                "ck_prescription_template_item_duration",
                "ck_prescription_template_item_quantity",
                "ck_prescription_template_item_quantity_unit",
            }.issubset(checks)
        )
        self.assertIn("uq_prescription_template_item_position", uniques)
