"""Rate-limited registration availability checks."""

import re
from ipaddress import ip_address
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from firebase_admin import auth, exceptions
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.core.rate_limits import Quota, RateLimitUnavailable, enforce_rate_limit
from app.database.session import get_session
from app.modules.identity.auth import _firebase_app
from app.modules.identity.models import User

router = APIRouter(tags=["Registration"])


class AvailabilityRequest(BaseModel):
    field: Literal["email", "phone"]
    value: str = Field(min_length=1, max_length=254)


@router.post("/registration/availability")
async def availability(
    data: AvailabilityRequest,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
) -> dict[str, bool]:
    if request.client is None:
        raise RateLimitUnavailable
    try:
        peer = str(ip_address(request.client.host))
    except ValueError:
        raise RateLimitUnavailable from None
    await enforce_rate_limit(
        request, Quota("registration-availability", peer, 30), window_seconds=60
    )
    value = data.value.strip()
    if data.field == "email":
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
            raise HTTPException(422, "Invalid email.")
        condition = func.lower(User.email) == value.lower()
    else:
        if not re.fullmatch(r"(?:0\d{9}|\+94\d{9})", value):
            raise HTTPException(422, "Invalid mobile number.")
        canonical = "0" + value[3:] if value.startswith("+94") else value
        condition = func.regexp_replace(User.phone, "[^0-9]", "", "g").in_(
            [canonical, "94" + canonical[1:]]
        )
    response.headers["Cache-Control"] = "no-store"
    exists = await session.scalar(select(User.id).where(condition).limit(1)) is not None
    firebase_only = False
    if data.field == "email" and not exists:
        try:
            await run_in_threadpool(auth.get_user_by_email, value, app=_firebase_app())
            exists = True
            firebase_only = True
        except auth.UserNotFoundError:
            pass
        except (exceptions.FirebaseError, ValueError):
            raise HTTPException(503, "Email availability is temporarily unavailable.") from None
    return {"available": not exists, "recoverable": firebase_only}
