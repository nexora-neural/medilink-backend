"""Safe, contract-shaped document errors."""
from fastapi import Request
from fastapi.responses import JSONResponse


class DocumentError(Exception):
    def __init__(self, status_code: int, code: str, message: str):
        self.status_code = status_code
        self.code = code
        self.message = message


async def document_error_handler(request: Request, exc: DocumentError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"code": exc.code, "message": exc.message,
                 "field_errors": {}, "request_id": getattr(request.state, "request_id", "")},
        headers={"Cache-Control": "no-store"},
    )
