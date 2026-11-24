import os
import tempfile
import time
import unittest
from pathlib import Path

from app.modules.documents.reconcile import MIN_AGE_SECONDS, inventory, quarantine


class ReconciliationTests(unittest.TestCase):
    def test_inventory_and_quarantine_preserve_referenced_and_recent_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old, live, recent, missing = (c * 32 for c in "abcd")
            for key in (old, live, recent):
                (root / key).write_bytes(b"private")
            past = time.time() - MIN_AGE_SECONDS - 10
            for key in (old, live):
                os.utime(root / key, (past, past))
            candidates, missing_ids = inventory(
                root, {live: "live-id", missing: "missing-id"}, time.time()
            )
            self.assertEqual(candidates, [old])
            self.assertEqual(missing_ids, ["missing-id"])
            quarantine(root, candidates)
            self.assertEqual((root / ".quarantine" / old).read_bytes(), b"private")
            self.assertFalse((root / old).exists())
            self.assertTrue((root / live).exists())
            self.assertTrue((root / recent).exists())

    def test_never_overwrites_quarantined_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            key = "a" * 32
            (root / key).write_bytes(b"source")
            (root / ".quarantine").mkdir()
            (root / ".quarantine" / key).write_bytes(b"existing")
            with self.assertRaises(FileExistsError):
                quarantine(root, [key])
            self.assertEqual((root / key).read_bytes(), b"source")
            self.assertEqual((root / ".quarantine" / key).read_bytes(), b"existing")
