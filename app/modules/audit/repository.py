"""Database access methods for audit events."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.audit.models import AuditEvent
from app.modules.audit.schemas import AuditEventInput


async def append_event(session: AsyncSession, data: AuditEventInput) -> AuditEvent:
    """Stage a new audit event in the caller's transaction."""
    event = AuditEvent(
        actor_user_id=data.actor_user_id,
        actor_client_id=data.actor_client_id,
        action=data.action,
        target_type=data.target_type,
        target_id=data.target_id,
        outcome=data.outcome,
        request_id=data.request_id,
        metadata_=data.metadata,
    )
    session.add(event)
    await session.flush()
    return event
