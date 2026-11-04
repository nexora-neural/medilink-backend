from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.router import api_router
from app.core.config import settings
from app.database import models as _models  # noqa: F401 - register ORM metadata
from app.database.session import check_database_connection, close_db, init_db
from app.modules.identity.auth import AuthenticationError


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        await init_db()
        yield
    finally:
        await close_db()


app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    description="MediLink API development scaffold.",
    debug=False,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=["Accept", "Authorization", "Content-Type"],
    expose_headers=["X-Request-ID"],
    max_age=600,
)


@app.middleware("http")
async def assign_request_id(request: Request, call_next):
    request.state.request_id = str(uuid4())
    response = await call_next(request)
    response.headers["X-Request-ID"] = request.state.request_id
    return response


@app.exception_handler(AuthenticationError)
async def authentication_error_handler(request: Request, exc: AuthenticationError):
    return JSONResponse(
        status_code=exc.status_code,
        content=exc.detail,
        headers=exc.headers,
    )
@app.exception_handler(RequestValidationError)
async def validation_error_handler(
    request: Request, _exc: RequestValidationError
) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={
            "code": "validation_error",
            "message": "Invalid request input.",
            "field_errors": {},
            "request_id": request.state.request_id,
        },
    )

@app.exception_handler(Exception)
async def unexpected_error_handler(
    request: Request, _exc: Exception
) -> JSONResponse:
    request_id = getattr(request.state, "request_id", None) or str(uuid4())
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"},
        headers={"X-Request-ID": request_id},
    )


app.include_router(api_router, prefix=settings.api_v1_prefix)


@app.get("/health", tags=["health"])
async def health_check() -> dict[str, str]:
    """Return API process health without exposing configuration or secrets."""
    return {"status": "ok"}


@app.get("/health/database", tags=["health"])
async def database_health_check() -> dict[str, str]:
    """Check database connectivity without returning connection details."""
    await check_database_connection()
    return {"database": "ok"}