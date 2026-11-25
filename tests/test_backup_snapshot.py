import os
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

from app.modules.operations import backup_snapshot
from app.modules.operations.backup_crypto import EncryptingWriter, verify_stream


@unittest.skipUnless(os.name == "posix", "Backup publication requires POSIX file permissions")
class BackupPublicationTests(unittest.TestCase):
    def test_success_is_authenticated_and_never_overwrites_existing_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            storage = root / "storage"
            storage.mkdir(mode=0o700)
            backups = root / "backups"
            backups.mkdir(mode=0o700)
            key = os.urandom(32)

            async def fake_capture(output, secret, private_root):
                self.assertEqual(private_root, storage)
                writer = EncryptingWriter(output, secret)
                writer.write(b"synthetic archive")
                writer.finish()

            with (
                patch.object(backup_snapshot, "_root", return_value=storage),
                patch.object(backup_snapshot, "file_consistency_lock", return_value=nullcontext()),
                patch.object(backup_snapshot, "_capture", side_effect=fake_capture),
            ):
                target = backups / "backup.enc"
                backup_snapshot.capture_backup(target, key)
                with target.open("rb") as file:
                    verify_stream(file, key)
                before = target.read_bytes()
                with self.assertRaises(FileExistsError):
                    backup_snapshot.capture_backup(target, key)
                self.assertEqual(target.read_bytes(), before)
                self.assertEqual(list(backups.iterdir()), [target])

    def test_failure_removes_only_its_own_partial_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            storage = root / "storage"
            storage.mkdir(mode=0o700)
            backups = root / "backups"
            backups.mkdir(mode=0o700)
            existing = backups / "existing.enc"
            existing.write_bytes(b"untouched")

            async def fail_capture(output, secret, private_root):
                output.write(b"partial encrypted output")
                raise OSError("simulated failure")

            with (
                patch.object(backup_snapshot, "_root", return_value=storage),
                patch.object(backup_snapshot, "file_consistency_lock", return_value=nullcontext()),
                patch.object(backup_snapshot, "_capture", side_effect=fail_capture),
            ):
                with self.assertRaises(OSError):
                    backup_snapshot.capture_backup(backups / "new.enc", os.urandom(32))
            self.assertEqual(list(backups.iterdir()), [existing])
            self.assertEqual(existing.read_bytes(), b"untouched")
