from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel

from app.main import app


class ValidationPayload(BaseModel):
    attempt_count: int


class ValidationErrorTests(IsolatedAsyncioTestCase):
    async def test_invalid_body_does_not_echo_submitted_value(self):
        submitted_value = "synthetic-sensitive-value"

        with patch.object(app.router, "routes", app.router.routes.copy()):
            @app.post("/_test/validation")
            async def validate_payload(payload: ValidationPayload):
                return payload

            transport = ASGITransport(app=app, raise_app_exceptions=False)
            async with AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                response = await client.post(
                    "/_test/validation",
                    json={"attempt_count": submitted_value},
                )

        self.assertEqual(response.status_code, 422)
        self.assertTrue(response.headers.get("X-Request-ID"))
        self.assertNotIn(submitted_value, response.text)

        self.assertEqual(response.json()["code"], "VALIDATION_ERROR")
        self.assertEqual(response.json()["request_id"], response.headers["X-Request-ID"])