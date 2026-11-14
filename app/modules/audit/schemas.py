"""Validated inputs for append-only audit events."""

from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SAFE_METADATA_KEYS = frozenset(
    {
        "actor_type",
        "attempt_count",
        "changed_fields",
        "code",
        "consultation_id",
        "created",
        "database_included",
        "files_included",
        "has_query",
        "is_sample",
        "job_type",
        "method",
        "organization_id",
        "previous_status",
        "previous_assigned_at",
        "previous_assigned_by",
        "reason_code",
        "records_count",
        "restore_check_status",
        "result_count",
        "role",
        "route",
        "status",
        "status_code",
        "source",
        "supersedes_id",
        "type",
    }
)


class AuditEventInput(BaseModel):
    """Safe, minimal data required to append one audit event."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    actor_user_id: UUID | None = None
    actor_client_id: UUID | None = None
    action: str = Field(min_length=1, max_length=200, pattern=r"^[a-z][a-z0-9_.-]*$")
    target_type: str = Field(min_length=1, max_length=100, pattern=r"^[a-z][a-z0-9_.-]*$")
    target_id: UUID | None = None
    outcome: str = Field(pattern=r"^(success|denied|failed)$")
    request_id: str = Field(min_length=1, max_length=200)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_actor(self) -> "AuditEventInput":
        if self.actor_user_id is not None and self.actor_client_id is not None:
            raise ValueError("An audit event can have at most one actor.")
        return self

    @field_validator("metadata")
    @classmethod
    def validate_safe_metadata(cls, metadata: dict[str, Any]) -> dict[str, Any]:
        unexpected = set(metadata) - SAFE_METADATA_KEYS
        if unexpected:
            raise ValueError("Audit metadata contains unsupported fields.")
        for key, value in metadata.items():
            if key == "changed_fields":
                if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                    raise ValueError("changed_fields must contain field names only.")
            elif value is not None and not isinstance(value, (str, int, float, bool)):
                raise ValueError(f"Audit metadata field {key} must be a scalar value.")
            if isinstance(value, str) and len(value) > 200:
                raise ValueError(f"Audit metadata field {key} is too long.")
        return metadata
