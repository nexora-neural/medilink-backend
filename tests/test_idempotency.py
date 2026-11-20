import json
import unittest
from uuid import uuid4

from app.core.idempotency import (
    IdempotencyConflict, IdempotencyScope, file_digest, lock_and_replay,
    request_digest, save_success,
)


class Session:
    def __init__(self):
        self.rows = {}

    async def execute(self, statement, params):
        if "INSERT INTO api_idempotency_records" in str(statement):
            key = tuple(params[x] for x in ("actor_type", "actor", "operation", "target", "key"))
            self.rows[key] = {"request_digest": params["digest"],
                              "response_status": params["status"],
                              "response_body": json.loads(params["body"])}

    async def fetch(self, db, statement, **params):
        key = tuple(params[x] for x in ("actor_type", "actor", "operation", "target", "key"))
        return self.rows.get(key)


class ReplayTests(unittest.IsolatedAsyncioTestCase):
    async def test_replay_conflict_and_scope(self):
        db = Session()
        scope = IdempotencyScope("user", uuid4(), "create", "target", "abcdefgh")
        digest = request_digest({"a": 1, "b": 2})
        self.assertIsNone(await lock_and_replay(db, scope, digest, db.fetch))
        await save_success(db, scope, digest, 201, {"id": "one"})
        replay = await lock_and_replay(db, scope, request_digest({"b": 2, "a": 1}), db.fetch)
        self.assertEqual((replay["response_status"], replay["response_body"]), (201, {"id": "one"}))
        with self.assertRaises(IdempotencyConflict):
            await lock_and_replay(db, scope, request_digest({"a": 2}), db.fetch)
        for other in (IdempotencyScope("user", uuid4(), "create", "target", "abcdefgh"),
                      IdempotencyScope("user", scope.actor_id, "update", "target", "abcdefgh"),
                      IdempotencyScope("user", scope.actor_id, "create", "other", "abcdefgh")):
            self.assertIsNone(await lock_and_replay(db, other, digest, db.fetch))

    async def test_failure_does_not_save(self):
        db = Session()
        scope = IdempotencyScope("user", uuid4(), "create", "target", "abcdefgh")
        with self.assertRaises(ValueError):
            await save_success(db, scope, request_digest({}), 422, {})
        self.assertFalse(db.rows)

    def test_file_content_changes_digest(self):
        self.assertNotEqual(request_digest({}, files={"file": file_digest(b"one")}),
                            request_digest({}, files={"file": file_digest(b"two")}))
