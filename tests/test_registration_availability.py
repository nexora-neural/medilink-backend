from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException, Response
from firebase_admin import auth

from app.modules.profiles import availability as module


@pytest.mark.asyncio
async def test_availability_checks_existing_phone_and_sets_no_store(monkeypatch):
    monkeypatch.setattr(module, "enforce_rate_limit", AsyncMock())
    session = SimpleNamespace(scalar=AsyncMock(return_value="existing"))
    request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))
    response = Response()
    result = await module.availability(
        module.AvailabilityRequest(field="phone", value="+94771234567"), request, response, session
    )
    assert result == {"available": False, "recoverable": False}
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_availability_rejects_invalid_phone(monkeypatch):
    monkeypatch.setattr(module, "enforce_rate_limit", AsyncMock())
    request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))
    with pytest.raises(HTTPException) as error:
        await module.availability(
            module.AvailabilityRequest(field="phone", value="123"),
            request,
            Response(),
            SimpleNamespace(scalar=AsyncMock()),
        )
    assert error.value.status_code == 422


@pytest.mark.asyncio
async def test_email_existing_only_in_firebase_is_unavailable(monkeypatch):
    monkeypatch.setattr(module, "enforce_rate_limit", AsyncMock())
    monkeypatch.setattr(module, "_firebase_app", lambda: None)
    monkeypatch.setattr(module.auth, "get_user_by_email", lambda *_args, **_kwargs: object())
    session = SimpleNamespace(scalar=AsyncMock(return_value=None))
    request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))
    result = await module.availability(
        module.AvailabilityRequest(field="email", value="existing@example.com"),
        request,
        Response(),
        session,
    )
    assert result == {"available": False, "recoverable": True}


@pytest.mark.asyncio
async def test_email_absent_in_database_and_firebase_is_available(monkeypatch):
    monkeypatch.setattr(module, "enforce_rate_limit", AsyncMock())
    monkeypatch.setattr(module, "_firebase_app", lambda: None)

    def missing(*_args, **_kwargs):
        raise auth.UserNotFoundError("missing")

    monkeypatch.setattr(module.auth, "get_user_by_email", missing)
    result = await module.availability(
        module.AvailabilityRequest(field="email", value="new@example.com"),
        SimpleNamespace(client=SimpleNamespace(host="127.0.0.1")),
        Response(),
        SimpleNamespace(scalar=AsyncMock(return_value=None)),
    )
    assert result == {"available": True, "recoverable": False}
