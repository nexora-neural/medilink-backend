"""Rate-limit dependencies for authenticated backend operations."""

from ipaddress import IPv6Address, ip_address
from typing import Annotated, Any

from fastapi import Depends, Request

from app.core.config import settings
from app.core.rate_limits import Quota, RateLimitUnavailable, enforce_rate_limit
from app.modules.identity.auth import get_verified_identity

VerifiedIdentity = Annotated[dict[str, Any], Depends(get_verified_identity)]


async def require_authenticated_rate_limit(request: Request, identity: VerifiedIdentity) -> None:
    """Apply the shared account quota to ordinary authenticated operations."""
    await enforce_rate_limit(
        request,
        Quota("authenticated", identity["uid"], settings.rate_limit_authenticated_per_minute),
        window_seconds=60,
    )


async def require_resolution_rate_limit(request: Request, identity: VerifiedIdentity) -> None:
    """Apply both account and peer IP quotas to QR, claim and token resolution."""
    if request.client is None:
        raise RateLimitUnavailable
    try:
        peer = ip_address(request.client.host)
    except ValueError:
        raise RateLimitUnavailable from None
    if isinstance(peer, IPv6Address) and peer.ipv4_mapped is not None:
        peer = peer.ipv4_mapped
    await enforce_rate_limit(
        request,
        Quota(
            "resolution-account",
            identity["uid"],
            settings.rate_limit_resolution_per_account_per_minute,
        ),
        Quota("resolution-ip", str(peer), settings.rate_limit_resolution_per_ip_per_minute),
        window_seconds=60,
    )


async def require_upload_rate_limit(request: Request, identity: VerifiedIdentity) -> None:
    """Apply the shared hourly account quota to uploads and OCR starts."""
    await enforce_rate_limit(
        request,
        Quota("upload", identity["uid"], settings.rate_limit_uploads_per_account_per_hour),
        window_seconds=3600,
    )
