from fastapi import APIRouter

from app.database.session import check_database_connection
from app.modules.audit.router import router as audit_router
from app.modules.clinical_records.router import router as clinical_records_router
from app.modules.consent.router import router as consent_router
from app.modules.consultations.router import router as consultations_router
from app.modules.dispensing.router import router as dispensing_router
from app.modules.documents.router import router as documents_router
from app.modules.fhir.router import router as fhir_router
from app.modules.identity.router import router as identity_router
from app.modules.integrations.router import router as integrations_router
from app.modules.measurements.router import router as measurements_router
from app.modules.medication.router import router as medication_router
from app.modules.notifications.router import router as notifications_router
from app.modules.organizations.router import router as organizations_router
from app.modules.prescriptions.router import router as prescriptions_router
from app.modules.profiles.router import router as profiles_router
from app.modules.vaccinations.router import router as vaccinations_router
from app.modules.verification.router import router as verification_router

api_router = APIRouter()

for module_router in (
    identity_router,
    profiles_router,
    organizations_router,
    verification_router,
    consent_router,
    consultations_router,
    clinical_records_router,
    prescriptions_router,
    medication_router,
    dispensing_router,
    documents_router,
    measurements_router,
    vaccinations_router,
    notifications_router,
    audit_router,
    integrations_router,
    fhir_router,
):
    api_router.include_router(module_router)


@api_router.get("/health/database", tags=["health"])
async def database_health_check() -> dict[str, str]:
    await check_database_connection()
    return {"database": "ok"}
