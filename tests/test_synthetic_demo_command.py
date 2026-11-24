"""CLI safety tests for the local synthetic demo seed."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from app.database.seed_synthetic_journeys import demo_id


class SyntheticDemoCommandTests(unittest.TestCase):
    def run_command(self, directory, *args, app_env="test"):
        env = os.environ.copy()
        env.update(APP_ENV=app_env, PRIVATE_UPLOAD_DIR=directory)
        return subprocess.run(
            [sys.executable, "-m", "app.database.seed_synthetic_journeys", *args],
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )

    def test_requires_apply_and_rejects_production(self):
        with tempfile.TemporaryDirectory() as directory:
            for args, environment in (((), "test"), (("--apply",), "production")):
                result = self.run_command(directory, *args, app_env=environment)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_rejects_incompatible_existing_file_without_modification(self):
        with tempfile.TemporaryDirectory() as directory:
            existing = Path(directory) / demo_id("report-document").hex
            existing.write_bytes(b"pre-existing content")
            result = self.run_command(directory, "--apply")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("storage collision", result.stderr)
            self.assertEqual(existing.read_bytes(), b"pre-existing content")

    def test_requires_both_explicit_uids(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_command(directory, "--apply", "--patient-uid", "synthetic")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("provide both", result.stderr)
            self.assertEqual(list(Path(directory).iterdir()), [])


class SyntheticReportWriteTests(unittest.IsolatedAsyncioTestCase):
    async def test_partial_report_is_removed_after_write_failure(self):
        from unittest.mock import AsyncMock, MagicMock, patch

        from app.database import seed_synthetic_journeys as module

        class FailedWriter:
            def __init__(self, fd):
                self.fd = fd

            def __enter__(self):
                return self

            def write(self, data):
                os.write(self.fd, data[:16])
                raise OSError("synthetic disk failure")

            def __exit__(self, *args):
                os.close(self.fd)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / demo_id("report-document").hex
            with (
                patch.object(sys, "argv", ["seed", "--apply"]),
                patch.dict(os.environ, {"APP_ENV": "test"}),
                patch.object(module.storage, "_path", return_value=path),
                patch.object(module.os, "fdopen", side_effect=lambda fd, mode: FailedWriter(fd)),
                patch.object(module, "engine", MagicMock(dispose=AsyncMock())),
                self.assertRaisesRegex(OSError, "synthetic disk failure"),
            ):
                await module.main()
            self.assertFalse(path.exists())
