from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from fastapi import Depends, FastAPI
from firebase_admin import auth, exceptions
from httpx import ASGITransport, AsyncClient

from app.database.session import get_session
from app.modules.identity.auth import (
    get_current_user,
    get_verified_identity,
)


class FirebaseIdentityTests(IsolatedAsyncioTestCase):
    """Test Firebase token verification dependency."""

    def setUp(self):
        self.app = FastAPI()

        @self.app.get("/test")
        async def test_route(
            identity=Depends(get_verified_identity),
        ):
            return {"uid": identity["uid"]}

    async def request(self, authorization: str | None = None):
        headers = {}

        if authorization is not None:
            headers["Authorization"] = authorization

        async with AsyncClient(
            transport=ASGITransport(app=self.app),
            base_url="http://testserver",
        ) as client:
            return await client.get("/test", headers=headers)

    async def test_missing_token_is_rejected(self):
        response = await self.request()

        self.assertEqual(response.status_code, 401)

    async def test_non_bearer_authorization_is_rejected(self):
        response = await self.request("Basic example")

        self.assertEqual(response.status_code, 401)

    async def test_valid_token_returns_identity(self):
        with (
            patch(
                "app.modules.identity.auth._firebase_app",
                return_value=object(),
            ),
            patch(
                "app.modules.identity.auth.auth.verify_id_token",
                return_value={"uid": "test-user-123"},
            ) as verify_token,
        ):
            response = await self.request("Bearer valid-test-token")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"uid": "test-user-123"})
        self.assertTrue(verify_token.call_args.kwargs["check_revoked"])

    async def test_invalid_token_is_rejected(self):
        with (
            patch(
                "app.modules.identity.auth._firebase_app",
                return_value=object(),
            ),
            patch(
                "app.modules.identity.auth.auth.verify_id_token",
                side_effect=auth.InvalidIdTokenError("Invalid token"),
            ),
        ):
            response = await self.request("Bearer invalid-test-token")

        self.assertEqual(response.status_code, 401)

    async def test_expired_token_is_rejected(self):
        with (
            patch(
                "app.modules.identity.auth._firebase_app",
                return_value=object(),
            ),
            patch(
                "app.modules.identity.auth.auth.verify_id_token",
                side_effect=auth.ExpiredIdTokenError(
                    "Token expired",
                    cause=None,
                ),
            ),
        ):
            response = await self.request("Bearer expired-test-token")

        self.assertEqual(response.status_code, 401)

    async def test_revoked_token_is_rejected(self):
        with (
            patch(
                "app.modules.identity.auth._firebase_app",
                return_value=object(),
            ),
            patch(
                "app.modules.identity.auth.auth.verify_id_token",
                side_effect=auth.RevokedIdTokenError("Token revoked"),
            ),
        ):
            response = await self.request("Bearer revoked-test-token")

        self.assertEqual(response.status_code, 401)

    async def test_disabled_firebase_user_is_rejected(self):
        with (
            patch(
                "app.modules.identity.auth._firebase_app",
                return_value=object(),
            ),
            patch(
                "app.modules.identity.auth.auth.verify_id_token",
                side_effect=auth.UserDisabledError("User disabled"),
            ),
        ):
            response = await self.request("Bearer disabled-user-token")

        self.assertEqual(response.status_code, 401)

    async def test_token_without_uid_is_rejected(self):
        with (
            patch(
                "app.modules.identity.auth._firebase_app",
                return_value=object(),
            ),
            patch(
                "app.modules.identity.auth.auth.verify_id_token",
                return_value={},
            ),
        ):
            response = await self.request("Bearer missing-uid-token")

        self.assertEqual(response.status_code, 401)

    async def test_firebase_service_failure_returns_503(self):
        with (
            patch(
                "app.modules.identity.auth._firebase_app",
                return_value=object(),
            ),
            patch(
                "app.modules.identity.auth.auth.verify_id_token",
                side_effect=exceptions.UnavailableError(
                    "Firebase unavailable",
                ),
            ),
        ):
            response = await self.request("Bearer test-token")

        self.assertEqual(response.status_code, 503)


class LocalAccountTests(IsolatedAsyncioTestCase):
    """Test local account checks after identity verification."""

    def setUp(self):
        self.app = FastAPI()
        self.fake_session = SimpleNamespace(
            scalar=AsyncMock(),
        )

        async def override_verified_identity():
            return {"uid": "test-user-123"}

        async def override_session():
            yield self.fake_session

        self.app.dependency_overrides[get_verified_identity] = (
            override_verified_identity
        )
        self.app.dependency_overrides[get_session] = override_session

        @self.app.get("/test")
        async def test_route(
            user=Depends(get_current_user),
        ):
            return {"status": user.status}

    async def request(self):
        async with AsyncClient(
            transport=ASGITransport(app=self.app),
            base_url="http://testserver",
        ) as client:
            return await client.get("/test")

    async def test_active_account_is_allowed(self):
        self.fake_session.scalar.return_value = SimpleNamespace(
            status="active",
        )

        response = await self.request()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "active"})
        self.fake_session.scalar.assert_awaited_once()

    async def test_suspended_account_is_rejected(self):
        self.fake_session.scalar.return_value = SimpleNamespace(
            status="suspended",
        )

        response = await self.request()

        self.assertEqual(response.status_code, 403)
        self.fake_session.scalar.assert_awaited_once()

    async def test_disabled_account_is_rejected(self):
        self.fake_session.scalar.return_value = SimpleNamespace(
            status="disabled",
        )

        response = await self.request()

        self.assertEqual(response.status_code, 403)
        self.fake_session.scalar.assert_awaited_once()

    async def test_missing_local_account_is_rejected(self):
        self.fake_session.scalar.return_value = None

        response = await self.request()

        self.assertEqual(response.status_code, 403)
        self.fake_session.scalar.assert_awaited_once()

class AuthenticationResponseContractTests(IsolatedAsyncioTestCase):
    async def test_error_response_has_matching_request_id(self):
        from app.main import app

        @app.get("/__test_auth_contract")
        async def protected(identity=Depends(get_verified_identity)):
            return {"ok": True}

        try:
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://testserver",
            ) as client:
                response = await client.get("/__test_auth_contract")

            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.json()["code"], "AUTHENTICATION_REQUIRED")
            self.assertNotIn("detail", response.json())
            self.assertEqual(response.json()["field_errors"], {})
            self.assertTrue(response.json()["request_id"])
            self.assertEqual(response.json()["request_id"], response.headers["x-request-id"])
            self.assertEqual(response.headers["www-authenticate"], "Bearer")
        finally:
            app.router.routes[:] = [
                route for route in app.router.routes
                if getattr(route, "path", None) != "/__test_auth_contract"
            ]
