"""Authentication dependency for report-writing integration clients."""

import hashlib
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import APIKeyHeader
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.session import get_session
from app.modules.identity.auth import AuthenticationError
from app.modules.integrations.models import IntegrationClient
from app.modules.integrations.request_limits import require_integration_rate_limit

api_key_header = APIKeyHeader(
    name="X-API-Key", scheme_name="IntegrationKey", auto_error=False
)


def _unauthorized(request: Request) -> AuthenticationError:
    return AuthenticationError(
        status_code=401,
        detail={
            "code": "INVALID_INTEGRATION_CREDENTIAL",
            "message": "Integration credential is invalid or inactive.",
            "field_errors": {},
            "request_id": getattr(request.state, "request_id", ""),
        },
        headers={"WWW-Authenticate": "ApiKey"},
    )


async def get_active_integration_client(
    request: Request,
    credential: Annotated[str | None, Depends(api_key_header)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> IntegrationClient:
    """Resolve a current, unexpired client with the narrowly approved scope."""
    if not credential:
        raise _unauthorized(request)
    credential_hash = hashlib.sha256(credential.encode("utf-8")).hexdigest()
    client = await db.scalar(
        select(IntegrationClient).where(IntegrationClient.credential_hash == credential_hash)
    )
    now = datetime.now(UTC)
    if (
        client is None
        or client.status != "active"
        or client.expires_at <= now
        or "reports:write" not in client.scopes
    ):
        raise _unauthorized(request)
    request.state.audit_actor_client_id = client.id
    await require_integration_rate_limit(request, client.id)
    return client
