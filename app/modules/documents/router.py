"""ML-36 private document API."""
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile, status
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.database.session import get_session
from app.modules.documents import service, storage
from app.modules.documents.errors import DocumentError
from app.modules.documents.schemas import DocumentRead
from app.modules.documents.upload_limits import validate_document_upload
from app.modules.identity.auth import get_current_user
from app.modules.identity.models import User
from app.modules.identity.request_limits import require_upload_rate_limit

router = APIRouter(tags=["Documents"])
DB = Annotated[AsyncSession, Depends(get_session)]
Actor = Annotated[User, Depends(get_current_user)]


async def _validate(request: Request,
                    file: UploadFile) -> str | JSONResponse:
    return await validate_document_upload(request, file)


@router.post("/patients/{patient_id}/documents", response_model=DocumentRead,
             status_code=status.HTTP_201_CREATED,
             dependencies=[Depends(require_upload_rate_limit)])

async def upload_patient_document(
    patient_id: UUID, request: Request, db: DB, actor: Actor,
    file: Annotated[UploadFile, File()],
    document_type: Annotated[str, Form()],
    upload_request_id: Annotated[UUID | None, Form()] = None,
    verification_application_id: Annotated[UUID | None, Form()] = None,
    supersedes_id: Annotated[UUID | None, Form()] = None,
    correction_reason: Annotated[str | None, Form()] = None,
):
    media_type = await _validate(request, file)
    if isinstance(media_type, JSONResponse):
        return media_type
    return await service.upload_document(
        db,
        actor,
        file,
        media_type,
        document_type,
        request.state.request_id,
        patient_id=patient_id,
        upload_request_id=upload_request_id,
        verification_application_id=verification_application_id,
        supersedes_id=supersedes_id,
        correction_reason=correction_reason,
    )


@router.post("/me/verification-documents", response_model=DocumentRead,
             status_code=status.HTTP_201_CREATED,
             dependencies=[Depends(require_upload_rate_limit)])

async def upload_verification_document(
    request: Request, db: DB, actor: Actor,
    file: Annotated[UploadFile, File()],
    document_type: Annotated[str, Form()],
    upload_request_id: Annotated[UUID | None, Form()] = None,
    verification_application_id: Annotated[UUID | None, Form()] = None,
    supersedes_id: Annotated[UUID | None, Form()] = None,
):
    media_type = await _validate(request, file)
    if isinstance(media_type, JSONResponse):
        return media_type
    return await service.upload_document(
        db, actor, file, media_type, document_type, request.state.request_id,
        upload_request_id=upload_request_id,
        verification_application_id=verification_application_id, supersedes_id=supersedes_id,
    )


@router.get("/documents/{document_id}", response_model=DocumentRead)
async def get_document(document_id: UUID,
                       request: Request,
                       db: DB,
                       actor: Actor):
    return await service.permitted_document(db,
                                            actor,
                                            document_id,
                                            request.state.request_id)


@router.get("/documents/{document_id}/content")
async def download_document(document_id: UUID,
                            request: Request,
                            db: DB,
                            actor: Actor):
    doc = await service.permitted_document(db, actor,
                                           document_id,
                                           request.state.request_id,
                                           content=True)
    try:
        path = await run_in_threadpool(storage.get_download_path, doc.storage_key)

    except (OSError, ValueError):
        raise DocumentError(
            404,
            "RESOURCE_NOT_FOUND",
            "Document content is unavailable.",
        ) from None

    filename = doc.original_filename.replace('"', "").replace("\r", "").replace("\n", "")
    return FileResponse(path, media_type=doc.media_type, filename=filename,
                        content_disposition_type="attachment",
                        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
