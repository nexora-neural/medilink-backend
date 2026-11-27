import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from app.modules.operations.backup_runner import validate_key_location


def test_key_must_be_outside_backup_and_document_directories():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        backups, documents = root / "backups", root / "documents"
        backups.mkdir()
        documents.mkdir()
        with patch("app.modules.operations.backup_runner._root", return_value=documents):
            for parent in (backups, documents):
                key = parent / "key"
                key.touch()
                with pytest.raises(ValueError):
                    validate_key_location(backups, key)
            separate = root / "separate-key"
            separate.touch()
            validate_key_location(backups, separate)
