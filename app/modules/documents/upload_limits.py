"""File size and content checks for document uploads."""

from typing import BinaryIO

from fastapi import Request, UploadFile
from fastapi.responses import JSONResponse
from PIL import Image
from pypdf import PdfReader
from pypdf.errors import PdfReadError
from starlette.concurrency import run_in_threadpool

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
READ_CHUNK_BYTES = 64 * 1024
IMAGE_MEDIA_TYPES = {"JPEG": "image/jpeg", "PNG": "image/png"}


async def reject_oversized_upload(
    request: Request, upload: UploadFile
) -> JSONResponse | None:
    """Return a 413 response if the file exceeds 10 MiB."""
    size = upload.size
    if size is None:
        size = 0
        await upload.seek(0)
        try:
            while size <= MAX_UPLOAD_BYTES:
                chunk = await upload.read(READ_CHUNK_BYTES)
                if not chunk:
                    break
                size += len(chunk)
        finally:
            await upload.seek(0)

    if size <= MAX_UPLOAD_BYTES:
        return None

    return JSONResponse(
        status_code=413,
        content={
            "code": "FILE_TOO_LARGE",
            "message": "The file exceeds the 10 MiB limit.",
            "field_errors": {},
            "request_id": request.state.request_id,
        },
    )


def _detect_media_type(file: BinaryIO) -> str | None:
    """Read supported content and rewind the file for the caller."""
    file.seek(0)
    try:
        header = file.read(5)
        file.seek(0)

        if header == b"%PDF-":
            reader = PdfReader(file, strict=True)
            if not reader.is_encrypted and len(reader.pages) > 0:
                return "application/pdf"
            return None

        with Image.open(file, formats=("JPEG", "PNG")) as picture:
            pixel_limit = Image.MAX_IMAGE_PIXELS
            if pixel_limit is not None and picture.width * picture.height > pixel_limit:
                return None

            picture.load()
            return IMAGE_MEDIA_TYPES.get(picture.format)
    except (OSError, ValueError, PdfReadError, Image.DecompressionBombError):
        return None
    finally:
        file.seek(0)


async def validate_document_upload(
    request: Request, upload: UploadFile
) -> str | JSONResponse:
    """Return the detected media type or a contract-shaped rejection."""
    rejection = await reject_oversized_upload(request, upload)
    if rejection is not None:
        return rejection

    media_type = await run_in_threadpool(_detect_media_type, upload.file)
    if media_type is not None:
        return media_type

    return JSONResponse(
        status_code=415,
        content={
            "code": "UNSUPPORTED_FILE_TYPE",
            "message": "Upload a readable PDF, JPEG or PNG file.",
            "field_errors": {},
            "request_id": request.state.request_id,
        },
    )