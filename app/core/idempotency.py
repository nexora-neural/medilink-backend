"""Transactional replay for PostgreSQL-backed, JSON-response operations.

Call inside the same transaction as the business write. Authorize the actor before
replaying; validate the operation before saving. Do not commit in the callback.
Records are retained and remain replayable after expires_at; key reuse is forbidden.
"""
import hashlib
import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from uuid import UUID

from sqlalchemy import text


class IdempotencyConflict(Exception):
    """A scoped key was already used with a different payload."""


@dataclass(frozen=True)
class IdempotencyScope:
    actor_type: str
    actor_id: UUID
    operation: str
    target: str
    key: str

    def __post_init__(self):
        if self.actor_type not in ("user", "client"):
            raise ValueError("actor_type must be user or client")
        if not self.operation or not self.target or not 8 <= len(self.key) <= 128:
            raise ValueError("Invalid idempotency scope")

    def params(self):
        return {"actor_type": self.actor_type, "actor": self.actor_id,
                "operation": self.operation, "target": self.target, "key": self.key}


def request_digest(body: Any, *, files: dict[str, str] | None = None) -> str:
    """Hash a JSON-compatible body and optional field-name to SHA-256 file digests.

    Hash actual file bytes before calling; include every relevant file and input.
    """
    payload = body if files is None else {"body": body, "files": files}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=True, allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def file_digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


async def lock_and_replay(db, scope: IdempotencyScope, digest: str,
                          fetch_one: Callable[..., Awaitable[Any]]):
    """Serialize requests for a scope until transaction completion, then find replay."""
    params = scope.params()
    # Stable, framed JSON prevents ambiguity between adjacent scope fields.
    lock_input = json.dumps([scope.actor_type, str(scope.actor_id), scope.operation,
                             scope.target, scope.key], separators=(",", ":"))
    lock_id = int.from_bytes(hashlib.sha256(lock_input.encode()).digest()[:8],
                             byteorder="big", signed=True)
    await db.execute(text("SELECT pg_advisory_xact_lock(:lock_id)"), {"lock_id": lock_id})
    previous = await fetch_one(db, """
        SELECT request_digest, response_status, response_body FROM api_idempotency_records
        WHERE actor_type = :actor_type AND actor_id = :actor
          AND operation_key = :operation AND target_key = :target AND idempotency_key = :key
    """, **params)
    if previous is not None and previous["request_digest"] != digest:
        raise IdempotencyConflict("This key was used with a different request.")
    return previous


async def save_success(db, scope: IdempotencyScope, digest: str, status: int, body: Any):
    """Save a successful JSON response in the caller's existing transaction."""
    if not 200 <= status <= 299:
        raise ValueError("Only successful responses can be saved")
    await db.execute(text("""
        INSERT INTO api_idempotency_records (
            actor_type, actor_id, operation_key, target_key, idempotency_key,
            request_digest, response_status, response_body, expires_at
        ) VALUES (
            :actor_type, :actor, :operation, :target, :key,
            :digest, :status, CAST(:body AS jsonb), 'infinity'::timestamptz
        )
    """), {**scope.params(), "digest": digest, "status": status,
           "body": json.dumps(body, ensure_ascii=True, allow_nan=False)})
