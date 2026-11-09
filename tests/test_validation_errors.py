from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

from httpx import ASGITransport, AsyncClient, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.main import app


class ValidationPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attempt_count: int
    metadata: dict[str, int] = Field(default_factory=dict)

    @field_validator("attempt_count")
    @classmethod
    def validate_attempt_count(cls, value: int) -> int:
        if value < 0:
            raise ValueError("synthetic-validator-secret")
        return value


class ValidationErrorTests(IsolatedAsyncioTestCase):
    async def request(self, payload: dict[str, object]) -> Response:
        with patch.object(app.router, "routes", app.router.routes.copy()):

            @app.post("/_test/validation")
            async def validate_payload(payload: ValidationPayload):
                return payload

            transport = ASGITransport(app=app, raise_app_exceptions=True)
            async with AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as client:
                return await client.post("/_test/validation", json=payload)

    async def test_invalid_body_does_not_echo_submitted_value(self):
        submitted_value = "synthetic-sensitive-value"
        response = await self.request({"attempt_count": submitted_value})

        self.assertEqual(response.status_code, 422)
        self.assertNotIn(submitted_value, response.text)
        self.assertEqual(response.json()["code"], "VALIDATION_ERROR")
        self.assertTrue(response.headers.get("X-Request-ID"))
        self.assertEqual(
            response.json()["request_id"],
            response.headers["X-Request-ID"],
        )
        self.assertEqual(
            response.json()["field_errors"],
            {"attempt_count": ["Invalid value."]},
        )

    async def test_unknown_field_name_is_not_exposed(self):
        sensitive_key = "synthetic-unknown-field-secret"
        response = await self.request(
            {"attempt_count": 1, sensitive_key: "unused"}
        )

        self.assertEqual(response.status_code, 422)
        self.assertNotIn(sensitive_key, response.text)

    async def test_dictionary_key_is_not_exposed(self):
        sensitive_key = "synthetic-dictionary-key-secret"
        response = await self.request(
            {
                "attempt_count": 1,
                "metadata": {sensitive_key: "invalid-integer"},
            }
        )

        self.assertEqual(response.status_code, 422)
        self.assertNotIn(sensitive_key, response.text)
        self.assertNotIn("invalid-integer", response.text)

    async def test_custom_validator_message_is_not_exposed(self):
        response = await self.request({"attempt_count": -1})

        self.assertEqual(response.status_code, 422)
        self.assertNotIn("synthetic-validator-secret", response.text)
        self.assertEqual(
            response.json()["field_errors"],
            {"attempt_count": ["Invalid value."]},
        )