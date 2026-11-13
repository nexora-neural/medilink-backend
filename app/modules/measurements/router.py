import logging
from typing import Annotated
from uuid import UUID, uuid4

import firebase_admin
from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from firebase_admin import auth
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.session import get_session

from .repository import fetch_one, list_active
from .schemas import (
    MeasurementCorrectionInput, MeasurementInput, MeasurementType,
    MeasurementsRead, MeasurementsReadPage,
)
from .service import (
    MeasurementError, correct_measurement, create_measurement, measurement_access,
)

logger = logging.getLogger(__name__)


class Error(BaseModel):
    code: str
    message: str
    field_errors: dict[str, list[str]]
    request_id: str


class MeasurementRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def handle(request: Request):
            request_id = str(uuid4())
            request.state.measurement_request_id = request_id
            headers = {"X-Request-ID": request_id, "Cache-Control": "no-store"}
            fields = {}
            try:
                response = await handler(request)
                response.headers.update(headers)
                return response
            except MeasurementError as exc:
                status, code, message = exc.status, exc.code, str(exc)
            except RequestValidationError as exc:
                malformed = any(error["type"] == "json_invalid" for error in exc.errors())
                status = 400 if malformed else 422
                code = "BAD_REQUEST" if malformed else "VALIDATION_ERROR"
                message = "Malformed JSON." if malformed else "Check the request fields."
                for error in exc.errors():
                    field = ".".join(str(part) for part in error["loc"])
                    fields.setdefault(field, []).append(error["msg"])
            except Exception:
                logger.error("Measurement request failed [request_id=%s]", request_id)
                status, code, message = 500, "INTERNAL_ERROR", "Unexpected failure."
            if status == 401:
                headers["WWW-Authenticate"] = "Bearer"
            return JSONResponse(
                status_code=status, headers=headers,
                content=Error(code=code, message=message, field_errors=fields,
                              request_id=request_id).model_dump(),
            )

        return handle


router = APIRouter(
    tags=["Measurements"], route_class=MeasurementRoute,
    responses={code: {"model": Error} for code in (400, 401, 403, 404, 409, 422, 429, 500)},
)
bearer = HTTPBearer(scheme_name="FirebaseBearer", auto_error=False)
Database = Annotated[AsyncSession, Depends(get_session)]


async def authenticated_uid(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> str:
    if credentials is None:
        raise MeasurementError(401, "UNAUTHENTICATED", "A Firebase ID token is required.")
    firebase_app = firebase_admin.get_app()
    try:
        claims = await run_in_threadpool(
            auth.verify_id_token, credentials.credentials, app=firebase_app, check_revoked=True,
        )
    except (auth.InvalidIdTokenError, auth.ExpiredIdTokenError,
            auth.RevokedIdTokenError, auth.UserDisabledError, auth.UserNotFoundError) as exc:
        raise MeasurementError(401, "UNAUTHENTICATED", "The Firebase ID token is invalid.") from exc
    return claims["uid"]


FirebaseUID = Annotated[str, Depends(authenticated_uid)]


async def active_actor(db, uid):
    actor = await fetch_one(db, """
        SELECT u.id, u.status,
            EXISTS (
                SELECT 1 FROM user_roles ur JOIN roles r ON r.id = ur.role_id
                WHERE ur.user_id = u.id AND r.code = 'patient'
            ) AS is_patient,
            EXISTS (
                SELECT 1 FROM user_roles ur JOIN roles r ON r.id = ur.role_id
                WHERE ur.user_id = u.id AND r.code = 'doctor'
            ) AS is_doctor,
            EXISTS (
                SELECT 1 FROM user_roles ur JOIN roles r ON r.id = ur.role_id
                JOIN professional_profiles p ON p.user_id = ur.user_id
                WHERE ur.user_id = u.id AND r.code = 'doctor'
                  AND p.role = 'doctor' AND p.verification_status = 'approved'
            ) AS is_verified_doctor
        FROM users u WHERE u.firebase_uid = :uid
        FOR SHARE OF u
    """, uid=uid)
    if actor is None or actor["status"] != "active":
        raise MeasurementError(403, "FORBIDDEN", "An active MediLink account is required.")
    return actor


class MeasurementFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)
    type: MeasurementType | None = None
    start: AwareDatetime | None = Field(default=None, alias="from")
    end: AwareDatetime | None = Field(default=None, alias="to")

    @field_validator("start", "end", mode="before")
    @classmethod
    def validate_timestamp(cls, value):
        if not isinstance(value, str) or "T" not in value.upper():
            raise ValueError("Use an RFC3339 timestamp with a timezone.")
        return value

    @model_validator(mode="after")
    def validate_window(self):
        if (self.start is None) != (self.end is None):
            raise ValueError("Provide both from and to.")
        if self.start is not None and self.start >= self.end:
            raise ValueError("from must precede to.")
        return self


@router.get("/patients/{patient_id}/measurements",
            response_model=MeasurementsReadPage, operation_id="medilink_031")
async def list_measurements(
    patient_id: UUID, filters: Annotated[MeasurementFilters, Query()],
    db: Database, uid: FirebaseUID,
):
    async with db.begin():
        actor = await active_actor(db, uid)
        await measurement_access(db, actor, patient_id)
        page = await list_active(
            db, patient_id, filters.type, filters.start, filters.end,
            filters.page, filters.page_size,
        )
        return MeasurementsReadPage.model_validate(page)


@router.post("/patients/{patient_id}/measurements",
             response_model=MeasurementsRead, status_code=201, operation_id="medilink_032")
async def post_measurement(
    patient_id: UUID, body: MeasurementInput, request: Request,
    db: Database, uid: FirebaseUID,
):
    async with db.begin():
        actor = await active_actor(db, uid)
        return await create_measurement(
            db, actor, patient_id, body, request.state.measurement_request_id,
        )


@router.post("/measurements/{measurement_id}/corrections",
             response_model=MeasurementsRead, status_code=201,
             operation_id="correctPatientMeasurement")
async def post_correction(
    measurement_id: UUID, body: MeasurementCorrectionInput, request: Request,
    key: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=128)],
    db: Database, uid: FirebaseUID,
):
    async with db.begin():
        actor = await active_actor(db, uid)
        return await correct_measurement(
            db, actor, measurement_id, body, key, request.state.measurement_request_id,
            await request.json(),
        )
