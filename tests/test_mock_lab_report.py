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

        def submit(url, **kwargs):
            kwargs["files"]["file"][1].seek(0)
            self.assertEqual(kwargs["files"]["file"][1].read(), b"synthetic-content")
            return response

        with (
            patch.dict(
                os.environ,
                {
                    "MEDILINK_LAB_API_KEY": "synthetic-secret",
                    "MEDILINK_LAB_REQUEST_TOKEN": "synthetic-token",
                },
            ),
            patch("sys.argv", self.args),
            patch.object(mock_lab_report.httpx, "post", side_effect=submit) as post,
        ):
            self.assertEqual(mock_lab_report.main(), 0)
        self.assertEqual(post.call_args.kwargs["headers"]["X-API-Key"], "synthetic-secret")
        self.assertEqual(post.call_args.kwargs["headers"]["Idempotency-Key"], "synthetic-key")
        self.assertEqual(post.call_args.kwargs["files"]["file"][2], "image/png")
        self.assertEqual(post.call_args.kwargs["data"]["request_token"], "synthetic-token")
        self.assertTrue(post.call_args.args[0].endswith("/api/v1/integrations/reports"))

    def test_remote_file_url_is_not_fetched(self):
        args = self.args.copy()
        args[args.index("--file") + 1] = "https://example.invalid/report.pdf"
        with (
            patch.dict(
                os.environ,
                {
                    "MEDILINK_LAB_API_KEY": "synthetic-secret",
                    "MEDILINK_LAB_REQUEST_TOKEN": "synthetic-token",
                },
            ),
            patch("sys.argv", args),
            patch.object(mock_lab_report.httpx, "post") as post,
        ):
            self.assertEqual(mock_lab_report.main(), 1)
        post.assert_not_called()

    def test_missing_local_credentials_stop_before_request(self):
        for environment in (
            {"MEDILINK_LAB_API_KEY": "", "MEDILINK_LAB_REQUEST_TOKEN": "synthetic-token"},
            {"MEDILINK_LAB_API_KEY": "synthetic-secret", "MEDILINK_LAB_REQUEST_TOKEN": ""},
        ):
            with self.subTest(missing=[key for key, value in environment.items() if not value]):
                with (
                    patch.dict(os.environ, environment),
                    patch("sys.argv", self.args),
                    patch.object(mock_lab_report.httpx, "post") as post,
                    self.assertRaises(SystemExit) as raised,
                ):
                    mock_lab_report.main()
                self.assertEqual(raised.exception.code, 2)
                post.assert_not_called()

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
