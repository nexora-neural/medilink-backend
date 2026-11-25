import hashlib
import io
import os
import tarfile
import tempfile
import unittest
from pathlib import Path

from app.modules.operations.backup_archive import DocumentFile, write_bundle
from app.modules.operations.backup_crypto import EncryptingWriter, encrypt_stream
from app.modules.operations.backup_restore import prepare_restore


@unittest.skipUnless(os.name == "posix", "Backup restore requires POSIX file permissions")
class RestoreTests(unittest.TestCase):
    def test_valid_backup_and_wrong_key(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data, storage_key, secret = b"private synthetic data", "a" * 32, os.urandom(32)
            (root / storage_key).write_bytes(data)
            encrypted = io.BytesIO()
            writer = EncryptingWriter(encrypted, secret)
            write_bundle(
                writer,
                io.BytesIO(b"synthetic dump"),
                [DocumentFile(storage_key, len(data), hashlib.sha256(data).hexdigest())],
                root,
            )
            writer.finish()
            backup = root / "backup"
            backup.write_bytes(encrypted.getvalue())
            with self.assertRaises(Exception):
                prepare_restore(backup, os.urandom(32), root)
            self.assertFalse(list(root.glob("medilink-restore-*")))
            stage = prepare_restore(backup, secret, root)
            self.assertEqual((stage / "database.dump").read_bytes(), b"synthetic dump")
            self.assertEqual((stage / "files" / storage_key).read_bytes(), data)

    def test_authenticated_path_traversal_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root, secret = Path(directory), os.urandom(32)
            payload = io.BytesIO()
            with tarfile.open(fileobj=payload, mode="w") as archive:
                member = tarfile.TarInfo("../escape")
                member.size = 1
                archive.addfile(member, io.BytesIO(b"x"))
            encrypted = io.BytesIO()
            encrypt_stream(io.BytesIO(payload.getvalue()), encrypted, secret)
            backup = root / "backup"
            backup.write_bytes(encrypted.getvalue())
            with self.assertRaises(ValueError):
                prepare_restore(backup, secret, root)
            self.assertFalse(list(root.glob("medilink-restore-*")))
