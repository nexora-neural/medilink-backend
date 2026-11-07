from io import BytesIO
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

from fastapi import Request, UploadFile
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient, Response
from PIL import Image
from pypdf import PdfWriter

from app.main import app
from app.modules.documents.upload_limits import validate_document_upload


class UploadTypeTests(IsolatedAsyncioTestCase):
    async def upload(
        self,
        data: bytes,
        *,
        filename: str = "synthetic.txt",
        content_type: str = "text/plain",
    ) -> Response:
        with patch.object(app.router, "routes", app.router.routes.copy()):

            @app.post("/_test/upload-type")
            async def check_upload(request: Request, file: UploadFile):
                result = await validate_document_upload(request, file)
                if isinstance(result, JSONResponse):
                    return result

                return {"media_type": result}

            async with AsyncClient(
                transport=ASGITransport(app=app, raise_app_exceptions=True),
                base_url="http://testserver",
            ) as client:
                return await client.post(
                    "/_test/upload-type",
                    files={"file": (filename, data, content_type)},
                )

    async def test_detects_jpeg_and_png_from_content(self):
        for image_format, media_type in (("JPEG", "image/jpeg"), ("PNG", "image/png")):
            with self.subTest(image_format=image_format):
                with BytesIO() as stream, Image.new("RGB", (4, 4)) as image:
                    image.save(stream, format=image_format)
                    response = await self.upload(stream.getvalue())

                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["media_type"], media_type)

    async def test_detects_readable_pdf_from_content(self):
        with BytesIO() as stream:
            writer = PdfWriter()
            writer.add_blank_page(width=72, height=72)
            writer.write(stream)
            response = await self.upload(stream.getvalue())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["media_type"], "application/pdf")

    async def test_fake_pdf_with_pdf_header_returns_safe_415(self):
        response = await self.upload(
            b"%PDF-1.7\nsynthetic-private-content",
            filename="synthetic.pdf",
            content_type="application/pdf",
        )

        self.assertEqual(response.status_code, 415)
        self.assertEqual(response.json()["code"], "UNSUPPORTED_FILE_TYPE")
        self.assertNotIn("synthetic-private-content", response.text)
        self.assertEqual(
            response.json()["request_id"],
            response.headers["X-Request-ID"],
        )