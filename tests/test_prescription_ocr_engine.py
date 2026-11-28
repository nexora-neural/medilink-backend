"""Offline OCR checks against synthetic printed prescription pages."""

import shutil
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from PIL import Image, ImageDraw, ImageFont

from app.core.config import settings
from app.modules.prescriptions.ocr_engine import (
    OcrProcessingError,
    extract_document_fields,
    extract_fields,
)


def _font():
    candidates = (
        Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    )
    for candidate in candidates:
        if candidate.is_file():
            return ImageFont.truetype(str(candidate), 42)
    return ImageFont.load_default(size=42)


def _printed_page():
    image = Image.new("RGB", (1700, 1200), "white")
    draw = ImageDraw.Draw(image)
    for number, line in enumerate(
        (
            "MEDILINK SYNTHETIC PRESCRIPTION",
            "Prescriber: Dr. Jane Example",
            "Start date: 2026-11-28",
            "End date: 2026-12-02",
            "Amoxicillin 500 mg - Take 1 capsule three times a day for 5 days",
        )
    ):
        draw.text((90, 100 + number * 150), line, fill="black", font=_font())
    return image


class PrescriptionOcrParserTests(unittest.TestCase):
    def test_compound_weight_based_dose_is_left_for_patient_review(self):
        directions = (
            "Take 1 mg/kg once a day for 5 days",
            "Take 1 mg / kg once a day for 5 days",
        )
        for instruction in directions:
            with self.subTest(instruction=instruction):
                result = extract_fields(f"Amoxicillin 500 mg - {instruction}")
                self.assertIsNone(result["items"][0]["dose_text"])

    def test_multiple_dose_steps_are_left_for_patient_review(self):
        directions = (
            "Take 1 tablet for 3 days, then 2 tablets three times a day",
            "Take 1 tablet three times a day for 3 days, then increase to 2 daily",
        )
        for instruction in directions:
            with self.subTest(instruction=instruction):
                result = extract_fields(f"Amoxicillin 500 mg - {instruction}")
                self.assertIsNone(result["items"][0]["dose_text"])

    def test_partially_matched_fractional_dose_is_left_for_patient_review(self):
        instructions = (
            "Take 1 tablet and a half once daily",
            "Take 1 tablet and ½ once daily",
        )
        for instruction in instructions:
            with self.subTest(instruction=instruction):
                result = extract_fields(f"Amoxicillin 500 mg - {instruction}")
                self.assertIsNone(result["items"][0]["dose_text"])

    def test_negated_dose_instruction_is_left_for_patient_review(self):
        result = extract_fields(
            "Amoxicillin 500 mg - Do not take 1 tablet once daily"
        )

        self.assertIsNone(result["items"][0]["dose_text"])


@unittest.skipUnless(
    shutil.which("tesseract") and shutil.which("pdftoppm"), "OCR tools unavailable"
)
class PrescriptionOcrRealEngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ml152-ocr-test-")
        self.addCleanup(self.temp.cleanup)
        self.old_storage = settings.private_upload_dir
        settings.private_upload_dir = str(Path(self.temp.name) / "private")
        self.addCleanup(setattr, settings, "private_upload_dir", self.old_storage)
        Path(settings.private_upload_dir).mkdir(mode=0o700)

    def save_as_private_document(self, extension: str, media_type: str) -> tuple[str, int]:
        key = uuid4().hex
        target = Path(settings.private_upload_dir) / key
        image = _printed_page()
        if extension == "pdf":
            image.save(target, format="PDF", resolution=300.0)
        else:
            image.save(target, format=extension.upper())
        return key, target.stat().st_size

    def test_real_extraction_supports_png_jpeg_and_pdf(self):
        for extension, media_type in (
            ("png", "image/png"),
            ("jpeg", "image/jpeg"),
            ("pdf", "application/pdf"),
        ):
            with self.subTest(extension=extension):
                key, size = self.save_as_private_document(extension, media_type)
                fields = extract_document_fields(
                    storage_key=key,
                    byte_size=size,
                    media_type=media_type,
                )
                self.assertEqual(fields["claimed_prescriber"], "Jane Example")
                self.assertEqual(fields["starts_on"], "2026-11-28")
                self.assertEqual(fields["ends_on"], "2026-12-02")
                self.assertEqual(fields["items"][0]["medicine_name_snapshot"], "Amoxicillin")
                self.assertEqual(fields["items"][0]["dose_text"], "1 capsule")
                self.assertEqual(fields["items"][0]["frequency"], "three times a day")
                self.assertNotIn("raw_text", fields)

    def test_dose_comes_from_directions_not_product_strength(self):
        result = extract_fields(
            "Amoxicillin 500 mg - Take 2 capsules three times a day for 5 days"
        )

        self.assertEqual(result["items"][0]["dose_text"], "2 capsules")

    def test_unclear_dose_is_left_for_patient_review(self):
        result = extract_fields(
            "Amoxicillin 500 mg - Take as directed three times a day for 5 days"
        )

        self.assertIsNone(result["items"][0]["dose_text"])

    def test_unavailable_engine_returns_a_stable_safe_code(self):
        key, size = self.save_as_private_document("png", "image/png")
        old_command = settings.prescription_ocr_tesseract_cmd
        settings.prescription_ocr_tesseract_cmd = "/path/does/not/exist/tesseract"
        self.addCleanup(setattr, settings, "prescription_ocr_tesseract_cmd", old_command)
        with self.assertRaises(OcrProcessingError) as caught:
            extract_document_fields(
                storage_key=key,
                byte_size=size,
                media_type="image/png",
            )
        self.assertEqual(caught.exception.code, "OCR_ENGINE_UNAVAILABLE")
        self.assertNotIn("/path/does/not/exist", str(caught.exception))

    def test_image_bytes_must_match_the_declared_media_type(self):
        key, size = self.save_as_private_document("jpeg", "image/jpeg")
        with self.assertRaises(OcrProcessingError) as caught:
            extract_document_fields(
                storage_key=key,
                byte_size=size,
                media_type="image/png",
            )
        self.assertEqual(caught.exception.code, "OCR_UNREADABLE")

    def test_parser_omits_ambiguous_unrecognized_fields(self):
        result = extract_fields("Name on document\nDrug X 2 mg - take with food")
        self.assertIsNone(result["claimed_prescriber"])
        self.assertIsNone(result["starts_on"])
        self.assertEqual(result["items"], [])
