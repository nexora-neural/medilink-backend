from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.router import api_router
from app.core.config import settings
from app.database.session import check_database_connection, close_db, init_db
from app.modules.audit import models as _audit_models
from app.modules.clinical_records import models as _clinical_records_models
from app.modules.consent import models as _consent_models
from app.modules.consultations import models as _consultations_models
from app.modules.dispensing import models as _dispensing_models
from app.modules.documents import models as _documents_models
from app.modules.fhir import models as _fhir_models
from app.modules.identity import models as _identity_models
from app.modules.integrations import models as _integrations_models
from app.modules.measurements import models as _measurements_models
from app.modules.medication import models as _medication_models
from app.modules.notifications import models as _notifications_models
from app.modules.organizations import models as _organizations_models
from app.modules.prescriptions import models as _prescriptions_models
from app.modules.profiles import models as _profiles_models
from app.modules.vaccinations import models as _vaccinations_models
from app.modules.verification import models as _verification_models


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield
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
