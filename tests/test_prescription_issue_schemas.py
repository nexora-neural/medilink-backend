"""Validation checks for complete doctor prescription content."""

from copy import deepcopy
from unittest import TestCase

from pydantic import ValidationError

from app.modules.prescriptions.schemas import (
    PrescriptionInput,
    PrescriptionIssueContent,
)


class PrescriptionIssueSchemaTests(TestCase):
    def valid_payload(self):
        return {
            "starts_on": "2026-11-25",
            "ends_on": "2026-11-27",
            "items": [
                {
                    "medicine_name_snapshot": "Synthetic medicine",
                    "dose_text": "Synthetic dose",
                    "route": "Oral",
                    "frequency": "Synthetic frequency",
                    "duration_days": 3,
                    "quantity": "3",
                    "quantity_unit": "tablet",
                    "instructions": "Synthetic interface testing only.",
                    "is_prn": False,
                }
            ],
        }

    def test_complete_content_is_accepted(self):
        result = PrescriptionIssueContent.model_validate(self.valid_payload())
        self.assertEqual(len(result.items), 1)
        self.assertEqual(result.items[0].route, "Oral")
        self.assertEqual(result.items[0].duration_days, 3)

    def test_issue_requires_at_least_one_item(self):
        payload = self.valid_payload()
        payload["items"] = []

        with self.assertRaises(ValidationError):
            PrescriptionIssueContent.model_validate(payload)

    def test_issue_requires_both_dates(self):
        for field in ("starts_on", "ends_on"):
            with self.subTest(field=field):
                payload = self.valid_payload()
                del payload[field]

                with self.assertRaises(ValidationError):
                    PrescriptionIssueContent.model_validate(payload)

    def test_end_date_cannot_precede_start_date(self):
        payload = self.valid_payload()
        payload["ends_on"] = "2026-11-24"

        with self.assertRaises(ValidationError):
            PrescriptionIssueContent.model_validate(payload)

    def test_issue_requires_complete_item_fields(self):
        fields = (
            "medicine_name_snapshot",
            "dose_text",
            "route",
            "frequency",
            "duration_days",
            "quantity",
            "quantity_unit",
            "is_prn",
        )

        for field in fields:
            with self.subTest(field=field):
                payload = self.valid_payload()
                del payload["items"][0][field]

                with self.assertRaises(ValidationError):
                    PrescriptionIssueContent.model_validate(payload)

    def test_blank_required_text_is_rejected(self):
        for field in (
            "medicine_name_snapshot",
            "dose_text",
            "route",
            "frequency",
        ):
            with self.subTest(field=field):
                payload = self.valid_payload()
                payload["items"][0][field] = "   "

                with self.assertRaises(ValidationError):
                    PrescriptionIssueContent.model_validate(payload)

    def test_unsupported_quantity_is_rejected(self):
        for quantity in (
            "0",
            "-1",
            "1.2345",
            "0.0001",
            "1000000000",
        ):
            with self.subTest(quantity=quantity):
                payload = self.valid_payload()
                payload["items"][0]["quantity"] = quantity

                with self.assertRaises(ValidationError):
                    PrescriptionIssueContent.model_validate(payload)

    def test_supported_decimal_quantity_is_preserved(self):
        payload = self.valid_payload()
        payload["items"][0]["quantity"] = "1.234"
        payload["items"][0]["quantity_unit"] = "mL"

        result = PrescriptionIssueContent.model_validate(payload)

        self.assertEqual(str(result.items[0].quantity), "1.234")

    def test_incomplete_content_remains_valid_as_draft(self):
        payload = deepcopy(self.valid_payload())
        payload.pop("starts_on")
        payload.pop("ends_on")

        for field in (
            "route",
            "duration_days",
            "quantity",
            "quantity_unit",
        ):
            payload["items"][0].pop(field)

        draft = PrescriptionInput.model_validate(payload)
        self.assertIsNone(draft.starts_on)
        self.assertIsNone(draft.items[0].quantity)

        with self.assertRaises(ValidationError):
            PrescriptionIssueContent.model_validate(payload)
