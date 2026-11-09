"""Request limits for authenticated integration clients."""

from uuid import UUID

from fastapi import Request

from app.core.config import settings
from app.core.rate_limits import Quota, enforce_rate_limit


async def require_integration_rate_limit(request: Request, client_id: UUID) -> None:
    """Apply the per-client quota using the ID resolved by API-key authentication."""
    await enforce_rate_limit(
        request,
        Quota("integration", str(client_id), settings.rate_limit_integration_per_client_per_minute),
        window_seconds=60,
    )
