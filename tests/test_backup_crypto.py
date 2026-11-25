import os
import tarfile
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from cryptography.exceptions import InvalidTag

from app.modules.operations.backup_crypto import (
    EncryptingWriter,
    decrypt_to_staging,
    encrypt_stream,
    read_key,
    verify_stream,
)


class BackupCryptoTests(unittest.TestCase):
    def test_encryption_rejects_payloads_above_gcm_limit(self):
        with patch("app.modules.operations.backup_crypto.MAX_PLAINTEXT_BYTES", 4):
            writer = EncryptingWriter(BytesIO(), os.urandom(32))
            writer.write(b"1234")
            with self.assertRaisesRegex(ValueError, "size limit"):
                writer.write(b"5")
            self.assertFalse(writer.finished)

    def test_short_writes_are_completed(self):
        class ShortWriter(BytesIO):
            def write(self, value):
                return super().write(value[:3])

        key = os.urandom(32)
        encrypted, restored = ShortWriter(), ShortWriter()
        encrypt_stream(BytesIO(b"synthetic data"), encrypted, key)
        decrypt_to_staging(BytesIO(encrypted.getvalue()), restored, key)
        self.assertEqual(restored.getvalue(), b"synthetic data")

    def test_authentication_rejects_wrong_key_tampering_and_truncation(self):
        key = os.urandom(32)
        output = BytesIO()
        encrypt_stream(BytesIO(b"synthetic database and file content"), output, key)
        encrypted = output.getvalue()
        self.assertNotIn(b"synthetic database", encrypted)
        verify_stream(BytesIO(encrypted), key)
        with self.assertRaises(InvalidTag):
            verify_stream(BytesIO(encrypted), os.urandom(32))
        changed = bytearray(encrypted)
        changed[-17] ^= 1
        with self.assertRaises(InvalidTag):
            verify_stream(BytesIO(changed), key)
        with self.assertRaises((InvalidTag, ValueError)):
            verify_stream(BytesIO(encrypted[:-1]), key)

    def test_key_file_requires_private_permissions_and_no_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "key"
            path.write_bytes(b"x" * 32)
            path.chmod(0o600)
            self.assertEqual(read_key(path), b"x" * 32)
            path.chmod(0o644)
            with self.assertRaises(PermissionError):
                read_key(path)
            link = Path(directory) / "link"
            link.symlink_to(path)
            with self.assertRaises(OSError):
                read_key(link)

    def test_backup_nonces_are_unique(self):
        first, second = BytesIO(), BytesIO()
        key = os.urandom(32)
        encrypt_stream(BytesIO(b"same"), first, key)
        encrypt_stream(BytesIO(b"same"), second, key)
        self.assertNotEqual(first.getvalue(), second.getvalue())

    def test_restore_requires_authentication_before_writing(self):
        key = os.urandom(32)
        encrypted = BytesIO()
        contents = os.urandom(1_100_000)
        encrypt_stream(BytesIO(contents), encrypted, key)
        output = BytesIO()
        decrypt_to_staging(BytesIO(encrypted.getvalue()), output, key)
        self.assertEqual(output.getvalue(), contents)
        output = BytesIO()
        with self.assertRaises(InvalidTag):
            decrypt_to_staging(BytesIO(encrypted.getvalue()), output, os.urandom(32))
        self.assertEqual(output.getvalue(), b"")

    def test_tar_stream_is_encrypted_and_authenticated(self):
        destination = BytesIO()
        key = os.urandom(32)
        writer = EncryptingWriter(destination, key)
        contents = b"synthetic private document"
        with tarfile.open(fileobj=writer, mode="w|") as archive:
            entry = tarfile.TarInfo("files/example")
            entry.size = len(contents)
            archive.addfile(entry, BytesIO(contents))
        writer.finish()
        self.assertNotIn(contents, destination.getvalue())
        verify_stream(BytesIO(destination.getvalue()), key)
        with self.assertRaises(ValueError):
            writer.write(b"more")
