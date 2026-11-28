"""Business rules and orchestration for notifications."""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.audit.service import append_event
from app.modules.identity.models import User
from app.modules.notifications import repository
from app.modules.notifications.schemas import NotificationsRead, NotificationsReadPage


class NotificationError(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status = status
        self.code = code
        self.message = message
        super().__init__(message)


async def list_own_notifications(
    session: AsyncSession,
    actor: User,
    *,
    unread: bool | None,
    page: int,
    page_size: int,
) -> NotificationsReadPage:
    rows, total = await repository.list_owned(
        session,
        actor.id,
        unread=unread,
        page=page,
        page_size=page_size,
    )
    return NotificationsReadPage(
        items=[NotificationsRead.model_validate(row, from_attributes=True) for row in rows],
        page=page,
        page_size=page_size,
        total=total,
    )


async def mark_notification_read(
    session: AsyncSession,
    actor: User,
    notification_id: UUID,
    request_id: str,
) -> NotificationsRead:
    try:
        changed_id = await repository.mark_unread_owned(session, actor.id, notification_id)
        notification = await repository.get_owned(session, actor.id, notification_id)
        if notification is None:
            raise NotificationError(
                404,
                "RESOURCE_NOT_FOUND",
                "Notification was not found.",
            )

        if changed_id is not None:
            await append_event(
                session,
                actor_user_id=actor.id,
                action="notification.read",
                target_type="notification",
                target_id=notification.id,
                outcome="success",
                request_id=request_id,
            )

        await session.commit()
        return NotificationsRead.model_validate(notification, from_attributes=True)
    except Exception:
        await session.rollback()
        raise
