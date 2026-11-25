"""Streaming authenticated encryption primitives for encrypted backups.

This module does not schedule or create a database/file snapshot.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import BinaryIO

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

MAGIC = b"MLBACKUP\x01"
NONCE_SIZE = 12
TAG_SIZE = 16
CHUNK_SIZE = 1024 * 1024
# GCM's per-nonce plaintext limit is 2^39 - 256 bits.
MAX_PLAINTEXT_BYTES = (1 << 36) - 32


def _write_all(destination: BinaryIO, data: bytes) -> None:
    view = memoryview(data)
    while view:
        count = destination.write(view)
        if count is None or count <= 0:
            raise OSError("Backup output write failed")
        view = view[count:]


def read_key(path: Path) -> bytes:
    """Read a 32-byte key from a private regular file without following symlinks."""
    if not hasattr(os, "O_NOFOLLOW"):
        raise OSError("This platform cannot safely open the backup key")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0))
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise PermissionError("Backup key must be a private regular file")
        if info.st_uid != os.geteuid():
            raise PermissionError("Backup key must be owned by the current user")
        with os.fdopen(fd, "rb", closefd=False) as key_file:
            key = key_file.read(33)
        if len(key) != 32:
            raise ValueError("Backup key must contain exactly 32 bytes")
        return key
    finally:
        os.close(fd)


class EncryptingWriter:
    """Forward plaintext to an encrypted destination without a plaintext file."""

    def __init__(self, destination: BinaryIO, key: bytes):
        if len(key) != 32:
            raise ValueError("Backup key must contain exactly 32 bytes")
        self.destination = destination
        nonce = os.urandom(NONCE_SIZE)
        header = MAGIC + nonce
        self.encryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
        self.encryptor.authenticate_additional_data(header)
        _write_all(self.destination, header)
        self.finished = False
        self.plaintext_bytes = 0

    def write(self, data: bytes) -> int:
        if self.finished:
            raise ValueError("Backup encryption is already finished")
        if self.plaintext_bytes + len(data) > MAX_PLAINTEXT_BYTES:
            raise ValueError("Backup exceeds the encryption format size limit")
        _write_all(self.destination, self.encryptor.update(data))
        self.plaintext_bytes += len(data)
        return len(data)

    def flush(self) -> None:
        self.destination.flush()

    def finish(self) -> None:
        if self.finished:
            raise ValueError("Backup encryption is already finished")
        _write_all(self.destination, self.encryptor.finalize())
        _write_all(self.destination, self.encryptor.tag)
        self.finished = True


def encrypt_stream(source: BinaryIO, destination: BinaryIO, key: bytes) -> None:
    """Write versioned AES-256-GCM ciphertext; the caller publishes on success."""
    writer = EncryptingWriter(destination, key)
    while chunk := source.read(CHUNK_SIZE):
        writer.write(chunk)
    writer.finish()


def _decrypt_pass(source: BinaryIO, key: bytes, destination: BinaryIO | None) -> None:
    if len(key) != 32:
        raise ValueError("Backup key must contain exactly 32 bytes")
    source.seek(0, os.SEEK_END)
    length = source.tell()
    header_size = len(MAGIC) + NONCE_SIZE
    if length < header_size + TAG_SIZE:
        raise ValueError("Truncated backup")
    if length - header_size - TAG_SIZE > MAX_PLAINTEXT_BYTES:
        raise ValueError("Backup exceeds the encryption format size limit")
    source.seek(0)
    header = source.read(header_size)
    if not header.startswith(MAGIC):
        raise ValueError("Unsupported backup format")
    source.seek(length - TAG_SIZE)
    tag = source.read(TAG_SIZE)
    decryptor = Cipher(algorithms.AES(key), modes.GCM(header[len(MAGIC):], tag)).decryptor()
    decryptor.authenticate_additional_data(header)
    source.seek(header_size)
    remaining = length - header_size - TAG_SIZE
    while remaining:
        chunk = source.read(min(CHUNK_SIZE, remaining))
        if not chunk:
            raise ValueError("Truncated backup")
        plaintext = decryptor.update(chunk)
        if destination is not None:
            _write_all(destination, plaintext)
        remaining -= len(chunk)
    decryptor.finalize()


def verify_stream(source: BinaryIO, key: bytes) -> None:
    """Authenticate a seekable encrypted archive without exposing plaintext."""
    _decrypt_pass(source, key, None)


def decrypt_to_staging(source: BinaryIO, destination: BinaryIO, key: bytes) -> None:
    """Authenticate first, then decrypt to a private disposable staging target.

    The caller must not publish or use the output until this function returns;
    the input may change between passes, so the second pass authenticates again.
    On any exception the caller must discard the staging target.
    """
    verify_stream(source, key)
    _decrypt_pass(source, key, destination)
