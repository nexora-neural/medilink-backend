from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

from fastapi import Request, UploadFile
from httpx import ASGITransport, AsyncClient, Response

from app.main import app
from app.modules.documents.upload_limits import reject_oversized_upload

CONTRACT_MAX_BYTES = 10_485_760


class UploadLimitTests(IsolatedAsyncioTestCase):
    async def upload(self, size: int) -> Response:
        with patch.object(app.router, "routes", app.router.routes.copy()):

            @app.post("/_test/upload-size")
            async def check_upload(request: Request, file: UploadFile):
                rejection = await reject_oversized_upload(request, file)
                if rejection is not None:
                    return rejection

                return {"accepted": True}

            transport = ASGITransport(app=app, raise_app_exceptions=True)
            async with AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as client:
                return await client.post(
                    "/_test/upload-size",
                    files={
                        "file": (
                            "synthetic-upload.bin",
                            b"x" * size,
                            "application/octet-stream",
                        )
                    },
                )

    async def test_file_at_exact_limit_is_accepted(self):
        response = await self.upload(CONTRACT_MAX_BYTES)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"accepted": True})

    async def test_file_one_byte_over_limit_returns_413(self):
        response = await self.upload(CONTRACT_MAX_BYTES + 1)

        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.json()["code"], "FILE_TOO_LARGE")
        self.assertEqual(
            response.json()["request_id"],
            response.headers["X-Request-ID"],
        )