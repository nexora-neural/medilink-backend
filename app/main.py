import logging
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.api.router import api_router
from app.core.config import settings
from app.core.rate_limits import (
    RateLimitExceeded,
    RateLimitUnavailable,
    rate_limit_exceeded_handler,
    rate_limit_unavailable_handler,
)
from app.database import models as _models  # noqa: F401 - register ORM metadata
from app.database.session import check_database_connection, close_db, init_db
from app.modules.identity.auth import AuthenticationError
from app.modules.documents.errors import (
    DocumentError,
    document_error_handler,
)

logger = logging.getLogger(__name__)


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
    expose_headers=["X-Request-ID", "Retry-After"],
    max_age=600,
)


@app.middleware("http")
async def assign_request_id(request: Request, call_next):
    request_id = str(uuid4())
    request.state.request_id = request_id

    try:
        response = await call_next(request)
    except Exception:
        logger.error("Unhandled API error (request_id=%s)", request_id)
        response = JSONResponse(
            status_code=500,
            content={
                "code": "INTERNAL_ERROR",
                "message": "Internal server error.",
                "field_errors": {},
                "request_id": request_id,
            },
        )

    response.headers["X-Request-ID"] = request_id
    if response.status_code >= 400:
        response.headers["Cache-Control"] = "no-store"
    return response


@app.exception_handler(AuthenticationError)
async def authentication_error_handler(
    request: Request, exc: AuthenticationError
):
    return JSONResponse(
        status_code=exc.status_code,
        content=exc.detail,
        headers=exc.headers,
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Return fixed messages using declared body, query and path field names."""
    route = request.scope.get("route")
    body_field = getattr(route, "body_field", None)
    field_info = getattr(body_field, "field_info", None)
    body_model = getattr(field_info, "annotation", None)

    known_fields: set[str] = set()
    if isinstance(body_model, type) and issubclass(body_model, BaseModel):
        for name, field in body_model.model_fields.items():
            known_fields.add(name)
            if isinstance(field.alias, str):
                known_fields.add(field.alias)
            if isinstance(field.validation_alias, str):
                known_fields.add(field.validation_alias)

    dependant = getattr(route, "dependant", None)
    parameters = {
        source: {field.alias for field in getattr(dependant, f"{source}_params", ())}
        for source in ("query", "path")
    }
    field_errors: dict[str, list[str]] = {}
    for error in exc.errors():
        location = error.get("loc", ())
        field_name = "request"
        if (
            len(location) >= 2
            and location[0] == "body"
            and isinstance(location[1], str)
            and location[1] in known_fields
        ):
            field_name = location[1]
        elif len(location) == 2 and location[1] in parameters.get(location[0], ()):
            field_name = f"{location[0]}.{location[1]}"

        field_errors[field_name] = ["Invalid value."]

    return JSONResponse(
        status_code=422,
        content={
            "code": "VALIDATION_ERROR",
            "message": "Invalid request input.",
            "field_errors": field_errors,
            "request_id": request.state.request_id,
        },
    )


app.add_exception_handler(
    RateLimitExceeded,
    rate_limit_exceeded_handler,
)

app.add_exception_handler(
    RateLimitUnavailable,
    rate_limit_unavailable_handler,
)

# Private document error handling
app.add_exception_handler(
    DocumentError,
    document_error_handler,
)

# Register API routes only once
app.include_router(
    api_router,
    prefix=settings.api_v1_prefix,
)


@app.get("/health", tags=["health"])
async def health_check() -> dict[str, str]:
    """Return API process health without exposing configuration or secrets."""
    return {"status": "ok"}


@app.get("/health/database", tags=["health"])
async def database_health_check() -> dict[str, str]:
    """Check database connectivity without returning connection details."""
    await check_database_connection()
    return {"database": "ok"}
