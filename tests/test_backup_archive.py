import hashlib
import json
import os
import tarfile
import tempfile
import unittest
from io import BytesIO
from pathlib import Path

from app.modules.operations.backup_archive import DocumentFile, write_bundle
from app.modules.operations.backup_crypto import EncryptingWriter, decrypt_to_staging


@unittest.skipUnless(os.name == "posix", "Backup archives require POSIX file operations")
class BackupArchiveTests(unittest.TestCase):
    def test_archive_includes_database_and_matching_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key = "a" * 32
            data = b"synthetic private file"
            (root / key).write_bytes(data)
            doc = DocumentFile(key, len(data), hashlib.sha256(data).hexdigest())
            encrypted, secret = BytesIO(), os.urandom(32)
            writer = EncryptingWriter(encrypted, secret)
            write_bundle(writer, BytesIO(b"synthetic database"), [doc], root)
            writer.finish()
            plaintext = BytesIO()
            decrypt_to_staging(BytesIO(encrypted.getvalue()), plaintext, secret)
            plaintext.seek(0)
            with tarfile.open(fileobj=plaintext, mode="r:") as archive:
                self.assertEqual(
                    archive.extractfile("database/00000000").read(), b"synthetic database"
                )
                self.assertEqual(archive.extractfile(f"files/{key}/00000000").read(), data)
                manifest = json.load(archive.extractfile("manifest.json"))
                self.assertEqual(manifest["documents"][0]["sha256"], doc.sha256)

    def test_missing_or_changed_file_fails_before_authentication_tag(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key = "a" * 32
            expected = b"expected"
            doc = DocumentFile(key, len(expected), hashlib.sha256(expected).hexdigest())
            for actual in (None, b"short", b"modified"):
                with self.subTest(actual=actual):
                    path = root / key
                    if actual is not None:
                        path.write_bytes(actual)
                    writer = EncryptingWriter(BytesIO(), os.urandom(32))
                    with self.assertRaises(OSError):
                        write_bundle(writer, BytesIO(b"db"), [doc], root)
                    self.assertFalse(writer.finished)
                    path.unlink(missing_ok=True)
