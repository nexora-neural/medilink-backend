"""Offline OCR for typed/printed English prescriptions.

The engine deliberately returns only contract fields. It does not retain raw OCR
text, decide clinical validity, or infer a doctor-issued prescription.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
import time
import warnings
from datetime import date
from pathlib import Path

from PIL import Image, ImageOps
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from app.core.config import settings
from app.modules.documents import storage

MAX_RENDERED_DIMENSION = 2480
_STRENGTH = r"\d+(?:\.\d+)?\s*(?:mg|mcg|µg|g|ml|mL|units?)"
_DOSE_AMOUNT = (
    r"(?:\d+(?:\.\d+)?(?:\s*[-–]\s*\d+(?:\.\d+)?)?"
    r"|a|an|one|two|three|four|five|six|half(?:\s+a)?|quarter)"
)
_DOSE_UNIT = (
    r"(?:tablets?|capsules?|pills?|caplets?|patches?|puffs?|drops?|"
    r"suppositories?|lozenges?|mL|ml|mg|mcg|µg|g|units?)"
)
_DOSE_QUANTITY = re.compile(
    rf"\b{_DOSE_AMOUNT}\s+{_DOSE_UNIT}(?![\w/])(?!\s*(?:/|per\b))",
    re.IGNORECASE,
)
_DOSE_DIRECTIVE = re.compile(
    rf"\b(?:take|use|apply|inject|inhale|instill|insert)\s+"
    rf"(?P<dose>{_DOSE_AMOUNT}\s+{_DOSE_UNIT})(?![\w/])(?!\s*(?:/|per\b))",
    re.IGNORECASE,
)
_DOSE_DIRECTIVE_START = re.compile(
    r"\b(?:take|use|apply|inject|inhale|instill|insert)\b",
    re.IGNORECASE,
)
_MULTI_STEP_DOSE = re.compile(r"\b(?:then|followed\s+by|alternating)\b", re.IGNORECASE)
_PARTIAL_DOSE_ADDITION = re.compile(
    r"\b(?:and|plus)\s+(?:(?:a|an)\s+)?"
    r"(?:(?:half|quarter|\d+(?:\.\d+)?|one|two|three|four|five|six)\b|[½¼¾⅓⅔])",
    re.IGNORECASE,
)
_NEGATED_DOSE_DIRECTIVE = re.compile(
    r"\b(?:(?:do|does|did)\s+not|don't|doesn't|didn't|never|not\s+to|"
    r"should\s+not|must\s+not)\s+(?:take|use|apply|inject|inhale|instill|insert)\b"
    r"|\bavoid\s+(?:taking|using|applying|injecting|inhaling|instilling|inserting)\b",
    re.IGNORECASE,
)
_PRESCRIBER = re.compile(r"(?im)^\s*(?:prescriber|doctor)\s*:\s*(?:dr\.?\s*)?(.+?)\s*$")
_DOCTOR_LINE = re.compile(r"(?im)^\s*dr\.?\s+([A-Z][A-Za-z .'-]{1,120})\s*$")
_ITEM_LINE = re.compile(
    rf"^\s*(?P<name>[A-Za-z][A-Za-z0-9 .()/',-]{{1,100}}?)\s+"
    rf"(?P<strength>{_STRENGTH})\s*(?:[-–:]\s*)(?P<directions>.+?)\s*$",
    re.IGNORECASE,
)
_FREQUENCY = re.compile(
    r"\b(?:once|twice|thrice|one|two|three|four)\s+(?:a\s+day|daily|per\s+day|times\s+(?:a|per)\s+day)\b"
    r"|\b(?:once|twice|three|four)\s+times\s+daily\b"
    r"|\bevery\s+\d+\s+hours?\b"
    r"|\bat\s+bedtime\b"
    r"|\b(?:as\s+needed|prn)\b",
    re.IGNORECASE,
)
_ISO_DATE = r"(\d{4}-\d{2}-\d{2})"
_START_DATE = re.compile(rf"(?i)\b(?:start|starts|from)\s+date\s*:?\s*{_ISO_DATE}")
_END_DATE = re.compile(rf"(?i)\b(?:end|ends|until|through)\s+date\s*:?\s*{_ISO_DATE}")
_DURATION = re.compile(r"\bfor\s+(\d{1,3})\s+days?\b", re.IGNORECASE)
_ROUTE = re.compile(r"\b(?:oral|by mouth|topical|intravenous|injection|inhaled)\b", re.IGNORECASE)


class OcrProcessingError(Exception):
    """A safe, allow-listed worker outcome; never contains OCR content or paths."""

    def __init__(self, code: str):
        if code not in {
            "OCR_ENGINE_UNAVAILABLE",
            "OCR_TIMEOUT",
            "OCR_UNREADABLE",
            "OCR_PROCESSING_FAILED",
        }:
            code = "OCR_PROCESSING_FAILED"
        self.code = code
        super().__init__(code)


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise OcrProcessingError("OCR_TIMEOUT")
    return remaining


def _run(command: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        raise OcrProcessingError("OCR_ENGINE_UNAVAILABLE") from None
    except subprocess.TimeoutExpired:
        raise OcrProcessingError("OCR_TIMEOUT") from None
    except OSError:
        raise OcrProcessingError("OCR_ENGINE_UNAVAILABLE") from None
    if result.returncode != 0:
        # A missing English language pack is an engine/configuration failure;
        # malformed documents are reported as unprocessable input.
        stderr = result.stderr.lower()
        if (
            "failed loading language" in stderr
            or "error opening data file" in stderr
        ):
            raise OcrProcessingError("OCR_ENGINE_UNAVAILABLE")
        raise OcrProcessingError("OCR_UNREADABLE")
    return result


def _ocr_image(image_path: Path, deadline: float) -> str:
    result = _run(
        [
            settings.prescription_ocr_tesseract_cmd,
            str(image_path),
            "stdout",
            "-l",
            "eng",
            "--psm",
            "6",
        ],
        timeout=_remaining(deadline),
    )
    return result.stdout


def _image_to_safe_png(source: Path, target: Path, *, expected_format: str) -> None:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(source, formats=("JPEG", "PNG")) as image:
                if image.format != expected_format:
                    raise OcrProcessingError("OCR_UNREADABLE")
                image.load()
                normalized = ImageOps.exif_transpose(image).convert("RGB")
                normalized.thumbnail((MAX_RENDERED_DIMENSION, MAX_RENDERED_DIMENSION))
                normalized.save(target, format="PNG", optimize=False)
    except (OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise OcrProcessingError("OCR_UNREADABLE") from None


def _extract_pdf(path: Path, temp_dir: Path, deadline: float) -> str:
    try:
        reader = PdfReader(path, strict=True)
        if reader.is_encrypted or not reader.pages:
            raise OcrProcessingError("OCR_UNREADABLE")
        page_count = len(reader.pages)
    except OcrProcessingError:
        raise
    except (OSError, ValueError, PdfReadError):
        raise OcrProcessingError("OCR_UNREADABLE") from None

    recognized_pages: list[str] = []
    for page_number in range(1, page_count + 1):
        prefix = temp_dir / f"page-{page_number}"
        _run(
            [
                settings.prescription_ocr_pdf_renderer_cmd,
                "-f",
                str(page_number),
                "-l",
                str(page_number),
                "-scale-to",
                str(MAX_RENDERED_DIMENSION),
                "-singlefile",
                "-png",
                str(path),
                str(prefix),
            ],
            timeout=_remaining(deadline),
        )
        page_image = prefix.with_suffix(".png")
        if not page_image.is_file():
            raise OcrProcessingError("OCR_UNREADABLE")
        recognized_pages.append(_ocr_image(page_image, deadline))
    return "\n".join(recognized_pages)


def _iso_date(match: re.Match[str] | None) -> date | None:
    if match is None:
        return None
    try:
        return date.fromisoformat(match.group(1))
    except ValueError:
        return None


def extract_fields(recognized_text: str) -> dict:
    """Map only conservative, high-confidence printed patterns to draft fields."""
    text = "\n".join(line.strip() for line in recognized_text.splitlines() if line.strip())
    if not text:
        raise OcrProcessingError("OCR_UNREADABLE")

    prescriber_match = _PRESCRIBER.search(text) or _DOCTOR_LINE.search(text)
    prescriber = prescriber_match.group(1).strip(" .:-")[:200] if prescriber_match else None
    starts_on = _iso_date(_START_DATE.search(text))
    ends_on = _iso_date(_END_DATE.search(text))
    if starts_on and ends_on and ends_on < starts_on:
        starts_on = ends_on = None

    items = []
    for line in text.splitlines():
        match = _ITEM_LINE.match(line)
        if match is None:
            continue
        directions = match.group("directions").strip()
        frequency_match = _FREQUENCY.search(directions)
        if frequency_match is None:
            continue
        frequency = re.sub(r"\s+", " ", frequency_match.group(0)).strip()
        # Only map a dose when the directions contain exactly one clear dose
        # and one administration directive. This avoids truncating compound
        # units (for example, 1 mg/kg) and silently losing later dose steps.
        dose_quantities = list(_DOSE_QUANTITY.finditer(directions))
        dose_directives = list(_DOSE_DIRECTIVE_START.finditer(directions))
        dose_matches = list(_DOSE_DIRECTIVE.finditer(directions))
        dose_match = (
            dose_matches[0]
            if (
                len(dose_quantities) == len(dose_directives) == len(dose_matches) == 1
                and _MULTI_STEP_DOSE.search(directions) is None
                and _PARTIAL_DOSE_ADDITION.search(directions) is None
                and _NEGATED_DOSE_DIRECTIVE.search(directions) is None
            )
            else None
        )
        dose = re.sub(r"\s+", " ", dose_match.group("dose")).strip() if dose_match else None
        duration_match = _DURATION.search(directions)
        duration = int(duration_match.group(1)) if duration_match else None
        if duration is not None and duration < 1:
            duration = None
        route_match = _ROUTE.search(directions)
        route = route_match.group(0).strip().lower() if route_match else None
        items.append(
            {
                "medicine_name_snapshot": re.sub(r"\s+", " ", match.group("name")).strip()[:200],
                # Product strength and administered dose are different values.
                # Never fill the dose from strength; null asks the patient to
                # enter it during the required review step.
                "dose_text": dose[:200] if dose else None,
                "route": route,
                "frequency": frequency[:200],
                "duration_days": duration,
                "instructions": directions[:2000],
                "quantity": None,
                "quantity_unit": None,
                "is_prn": bool(re.search(r"\b(?:prn|as\s+needed)\b", directions, re.IGNORECASE)),
            }
        )

    return {
        "claimed_prescriber": prescriber,
        "starts_on": starts_on.isoformat() if starts_on else None,
        "ends_on": ends_on.isoformat() if ends_on else None,
        "items": items,
    }


def extract_document_fields(*, storage_key: str, byte_size: int, media_type: str) -> dict:
    """Read a retained private document and run offline English OCR."""
    if media_type not in {"application/pdf", "image/jpeg", "image/png"}:
        raise OcrProcessingError("OCR_UNREADABLE")
    try:
        original = storage.get_download_path(storage_key, expected_size=byte_size)
    except (OSError, ValueError):
        raise OcrProcessingError("OCR_PROCESSING_FAILED") from None

    deadline = time.monotonic() + settings.prescription_ocr_timeout_seconds
    # The private temp folder is under the mode-0700 storage root, and is always
    # removed after the attempt. Original evidence is opened read-only.
    with tempfile.TemporaryDirectory(prefix="medilink-ocr-", dir=storage._root()) as folder:
        temp_dir = Path(folder)
        if media_type == "application/pdf":
            text = _extract_pdf(original, temp_dir, deadline)
        else:
            converted = temp_dir / "source.png"
            expected_format = "JPEG" if media_type == "image/jpeg" else "PNG"
            _image_to_safe_png(original, converted, expected_format=expected_format)
            text = _ocr_image(converted, deadline)
    return extract_fields(text)
