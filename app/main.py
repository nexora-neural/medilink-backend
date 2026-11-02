from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.router import api_router
from app.core.config import settings
from app.database import models as _models  # noqa: F401 - register ORM metadata
from app.database.session import check_database_connection, close_db, init_db


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
    debug=settings.debug,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=["Accept", "Authorization", "Content-Type"],
    max_age=600,
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
