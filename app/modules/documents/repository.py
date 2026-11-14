"""Database access methods for private documents."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.documents.models import Document
from app.modules.laboratory.models import LabUploadRequest, Report
from app.modules.prescriptions.core_models import Prescription
from app.modules.verification.models import VerificationApplication, VerificationApplicationDocument


async def document_by_id(session: AsyncSession, document_id: UUID, *, lock: bool = False):
    statement = select(Document).where(Document.id == document_id)
    return await session.scalar(statement.with_for_update() if lock else statement)


async def report_for_document(session: AsyncSession, document_id: UUID):
    return await session.scalar(select(Report).where(Report.document_id == document_id))


async def prescription_for_document(session: AsyncSession, document_id: UUID):
    return await session.scalar(
        select(Prescription).where(Prescription.original_document_id == document_id)
    )


async def verification_for_document(session: AsyncSession, document_id: UUID):
    return await session.scalar(
        select(VerificationApplication)
        .join(VerificationApplicationDocument,
              VerificationApplicationDocument.application_id == VerificationApplication.id)
        .where(VerificationApplicationDocument.document_id == document_id)
    )


async def lab_request(session: AsyncSession, request_id: UUID):
    return await session.scalar(
        select(LabUploadRequest).where(LabUploadRequest.id == request_id).with_for_update()
    )
