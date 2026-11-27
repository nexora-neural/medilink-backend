"""Tests for the mock laboratory client."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from scripts import mock_lab_report


class MockLabReportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "synthetic.png"
        self.path.write_bytes(b"synthetic-content")
        self.args = [
            "mock_lab_report",
            "--file",
            str(self.path),
            "--external-report-id",
            "external-1",
            "--idempotency-key",
            "synthetic-key",
            "--lab-reference",
            "ref-1",
            "--report-type",
            "synthetic",
        ]

    def test_submit_uses_api_key_and_file_type(self):
        response = httpx.Response(201, text="accepted")
        with (
            patch.dict(
                os.environ,
                {
                    "MEDILINK_LAB_API_KEY": "synthetic-secret",
                    "MEDILINK_LAB_REQUEST_TOKEN": "synthetic-token",
                },
            ),
            patch("sys.argv", self.args),
            patch.object(mock_lab_report.httpx, "post", return_value=response) as post,
        ):
            self.assertEqual(mock_lab_report.main(), 0)
        self.assertEqual(post.call_args.kwargs["headers"]["X-API-Key"], "synthetic-secret")
        self.assertEqual(post.call_args.kwargs["files"]["file"][2], "image/png")
        self.assertTrue(post.call_args.args[0].endswith("/api/v1/integrations/reports"))

    def test_correction_uses_version_path_and_returns_failure(self):
        response = httpx.Response(409, text="conflict")
        with (
            patch.dict(
                os.environ,
                {
                    "MEDILINK_LAB_API_KEY": "synthetic-secret",
                    "MEDILINK_LAB_REQUEST_TOKEN": "synthetic-token",
                },
            ),
            patch("sys.argv", self.args + ["--correct-report-id", "report-id", "--reason", "fix"]),
            patch.object(mock_lab_report.httpx, "post", return_value=response) as post,
        ):
            self.assertEqual(mock_lab_report.main(), 1)
        self.assertTrue(post.call_args.args[0].endswith("/report-id/versions"))
        self.assertEqual(post.call_args.kwargs["data"]["reason"], "fix")
