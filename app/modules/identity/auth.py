"""Firebase identity verification and local account authentication dependencies."""

from functools import lru_cache
from typing import Annotated, Any

import firebase_admin
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from firebase_admin import auth, exceptions
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.core.config import settings
from app.database.session import get_session
from app.modules.identity.models import User

bearer = HTTPBearer(auto_error=False)


class AuthenticationError(HTTPException):
    """Authentication failure with a contract-shaped response."""


def _error(request: Request, status_code: int, code: str, message: str) -> HTTPException:
    request_id = getattr(request.state, "request_id", "")
    return AuthenticationError(
        status_code=status_code,
        detail={"code": code, "message": message, "field_errors": {}, "request_id": request_id},
        headers={"WWW-Authenticate": "Bearer"} if status_code == 401 else None,
    )


@lru_cache(maxsize=1)
def _firebase_app() -> firebase_admin.App:
    try:
        return firebase_admin.get_app()
    except ValueError:
        options = (
            {"projectId": settings.firebase_project_id} if settings.firebase_project_id else None
        )
        return firebase_admin.initialize_app(options=options)


async def get_verified_identity(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> dict[str, Any]:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise _error(request, 401, "AUTHENTICATION_REQUIRED", "Authentication is required.")
    try:
        claims = await run_in_threadpool(
            auth.verify_id_token, credentials.credentials, app=_firebase_app(), check_revoked=True
        )
    except (auth.InvalidIdTokenError, auth.UserDisabledError, ValueError):
        raise _error(
            request, 401, "INVALID_TOKEN", "The authentication token is invalid."
        ) from None
    except exceptions.FirebaseError:
        raise _error(
            request, 503, "AUTHENTICATION_UNAVAILABLE",
            "Authentication is temporarily unavailable.",
        ) from None
    if not isinstance(claims.get("uid"), str) or not claims["uid"]:
        raise _error(request, 401, "INVALID_TOKEN", "The authentication token is invalid.")
    return claims


async def get_current_user(
    request: Request,
    identity: Annotated[dict[str, Any], Depends(get_verified_identity)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> User:
    user = await session.scalar(select(User).where(User.firebase_uid == identity["uid"]))
    if user is None:
        raise _error(request, 403, "ACCOUNT_INACTIVE", "The local account is not available.")
    if user.status in {"suspended", "disabled"}:
        raise _error(request, 403, "ACCOUNT_INACTIVE", "The local account is inactive.")
    return user
