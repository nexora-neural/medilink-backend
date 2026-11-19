from typing import Annotated

from fastapi import APIRouter, Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.session import get_session
from app.modules.identity.auth import get_current_user
from app.modules.identity.models import User
from app.modules.identity.request_limits import require_resolution_rate_limit
from app.modules.profiles.route import ProfileRoute

from . import service
from .schemas import DoctorQrRead, DoctorQrResolveRequest, ProfessionalIdentity

router = APIRouter(tags=["Doctor QR"], route_class=ProfileRoute)

DB = Annotated[AsyncSession, Depends(get_session)]
Actor = Annotated[User, Depends(get_current_user)]


@router.get("/me/doctor-qr", response_model=DoctorQrRead)
async def get_my_doctor_qr(
    db: DB,
    actor: Actor,
    response: Response,
) -> DoctorQrRead:
    response.headers["Cache-Control"] = "no-store"
    return await service.get_my_doctor_qr(db, actor)


@router.post(
    "/doctor-qr/resolve",
    response_model=ProfessionalIdentity,
    response_model_exclude_none=True,
    dependencies=[Depends(require_resolution_rate_limit)],
)
async def resolve_doctor_qr(
    payload: DoctorQrResolveRequest,
    db: DB,
    actor: Actor,
    response: Response,
) -> ProfessionalIdentity:
    response.headers["Cache-Control"] = "no-store"
    return await service.resolve_doctor_qr(db, actor, payload.doctor_reference)