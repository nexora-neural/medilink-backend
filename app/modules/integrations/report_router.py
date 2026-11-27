"""Multipart report ingestion endpoints for authenticated integration clients."""

from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Header, Request, UploadFile
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.session import get_session
from app.modules.documents.upload_limits import validate_document_upload
from app.modules.integrations.auth import get_active_integration_client
from app.modules.integrations.models import IntegrationClient
from app.modules.integrations.report_service import submit
from app.modules.integrations.schemas import IntegrationSubmissionRead
from app.modules.laboratory.router import LabUploadRequestRoute
from app.modules.laboratory.service import LabUploadRequestError

router = APIRouter(tags=["Integration reports"], route_class=LabUploadRequestRoute)
DB = Annotated[AsyncSession, Depends(get_session)]
Client = Annotated[IntegrationClient, Depends(get_active_integration_client)]


async def _submit(
    request,
    db,
    client,
    file,
    request_token,
    external_report_id,
    report_type,
    lab_reference,
    report_date,
    notes,
    key,
    report_id=None,
    reason=None,
):
    if any(
        not value.strip()
        for value in (request_token, external_report_id, report_type, lab_reference)
    ) or (reason is not None and not reason.strip()):
        raise LabUploadRequestError(422, "VALIDATION_ERROR", "Required fields cannot be blank.")
    media_type = await validate_document_upload(request, file)
    if isinstance(media_type, JSONResponse):
        return media_type
    fields = {
        "request_token": request_token,
        "external_report_id": external_report_id,
        "report_type": report_type,
        "report_date": report_date.isoformat() if report_date else None,
        "lab_reference": lab_reference,
        "notes": notes,
        "reason": reason,
    }
    return await submit(
        db, client, file, media_type, fields, key, request.state.request_id, report_id
    )


@router.post("/integrations/reports", status_code=201, response_model=IntegrationSubmissionRead)
async def create_report(
    request: Request,
    db: DB,
    client: Client,
    file: Annotated[UploadFile, File()],
    request_token: Annotated[str, Form(min_length=1, max_length=256)],
    external_report_id: Annotated[str, Form(min_length=1, max_length=150)],
    report_type: Annotated[str, Form(min_length=1, max_length=200)],
    lab_reference: Annotated[str, Form(min_length=1, max_length=150)],
    key: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=128)],
    report_date: Annotated[date | None, Form()] = None,
    notes: Annotated[str | None, Form(max_length=2000)] = None,
):
    return await _submit(
        request,
        db,
        client,
        file,
        request_token,
        external_report_id,
        report_type,
        lab_reference,
        report_date,
        notes,
        key,
    )


@router.post(
    "/integrations/reports/{report_id}/versions",
    status_code=201,
    response_model=IntegrationSubmissionRead,
)
async def correct_report(
    report_id: UUID,
    request: Request,
    db: DB,
    client: Client,
    file: Annotated[UploadFile, File()],
    request_token: Annotated[str, Form(min_length=1, max_length=256)],
    external_report_id: Annotated[str, Form(min_length=1, max_length=150)],
    report_type: Annotated[str, Form(min_length=1, max_length=200)],
    lab_reference: Annotated[str, Form(min_length=1, max_length=150)],
    reason: Annotated[str, Form(min_length=1, max_length=1000)],
    key: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=128)],
    report_date: Annotated[date | None, Form()] = None,
    notes: Annotated[str | None, Form(max_length=2000)] = None,
):
    return await _submit(
        request,
        db,
        client,
        file,
        request_token,
        external_report_id,
        report_type,
        lab_reference,
        report_date,
        notes,
        key,
        report_id,
        reason,
    )
