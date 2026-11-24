"""Focused smoke tests for ML-36 private file storage. No PostgreSQL required."""
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from fastapi import UploadFile

from app.core.config import settings
from app.modules.documents import storage


class StorageTests(unittest.IsolatedAsyncioTestCase):
    async def test_private_file_round_trip_and_cleanup(self):
        with tempfile.TemporaryDirectory() as root:
            with patch.object(settings, "private_upload_dir", root):
                file = UploadFile(file=BytesIO(b"example"), filename="private.pdf")
                key, size, digest = await storage.save(file)
                self.assertEqual(size, 7)
                self.assertEqual(len(key), 32)
                self.assertEqual(len(digest), 64)
                path = storage.get_download_path(key)
                self.assertEqual(path.read_bytes(), b"example")
                self.assertTrue(str(path).startswith(str(Path(root).resolve())))
                await storage.delete(key)
                self.assertFalse(path.exists())

    def test_rejects_path_traversal_key(self):
        with tempfile.TemporaryDirectory() as root:
            with patch.object(settings, "private_upload_dir", root):
                with self.assertRaises(ValueError):
                    storage.get_download_path("../sensitive.txt")

    def test_rejects_symlink_to_private_file(self):
        with tempfile.TemporaryDirectory() as root:
            with patch.object(settings, "private_upload_dir", root):
                target = Path(root) / "target"
                target.write_bytes(b"private")
                (Path(root) / ("a" * 32)).symlink_to(target)
                with self.assertRaises(OSError):
                    storage.get_download_path("a" * 32)

    async def test_rejects_empty_file(self):
        with tempfile.TemporaryDirectory() as root:
            with patch.object(settings, "private_upload_dir", root):
                file = UploadFile(file=BytesIO(b""), filename="empty.pdf")
                with self.assertRaises(ValueError):
                    await storage.save(file)
                self.assertEqual(list(Path(root).iterdir()), [])

    async def test_read_failure_removes_partial_file(self):
        class FailingFile(BytesIO):
            def read(self, size=-1):
                if self.tell():
                    raise OSError("simulated read failure")
                return super().read(3)

        with tempfile.TemporaryDirectory() as root:
            with patch.object(settings, "private_upload_dir", root):
                file = UploadFile(file=FailingFile(b"private content"), filename="x.pdf")
                with self.assertRaisesRegex(OSError, "simulated read failure"):
                    await storage.save(file)
                self.assertEqual(list(Path(root).iterdir()), [])
