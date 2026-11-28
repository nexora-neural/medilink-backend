"""Database access methods for notifications."""

from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.notifications.models import Notification


async def list_owned(
    session: AsyncSession,
    user_id: UUID,
    *,
    unread: bool | None,
    page: int,
    page_size: int,
) -> tuple[list[Notification], int]:
    conditions = [Notification.user_id == user_id]
    if unread is True:
        conditions.append(Notification.read_at.is_(None))
    elif unread is False:
        conditions.append(Notification.read_at.is_not(None))

    total = int(
        await session.scalar(select(func.count()).select_from(Notification).where(*conditions)) or 0
    )
    offset = min((page - 1) * page_size, 2**63 - 1)
    rows = await session.scalars(
        select(Notification)
        .where(*conditions)
        .order_by(Notification.created_at.desc(), Notification.id.desc())
        .offset(offset)
        .limit(page_size)
    )
    return list(rows.all()), total


async def mark_unread_owned(
    session: AsyncSession,
    user_id: UUID,
    notification_id: UUID,
) -> UUID | None:
    """Atomically set read_at once, scoped to the notification owner."""
    result = await session.execute(
        update(Notification)
        .where(
            Notification.id == notification_id,
            Notification.user_id == user_id,
            Notification.read_at.is_(None),
        )
        .values(read_at=func.clock_timestamp())
        .returning(Notification.id)
    )
    return result.scalar_one_or_none()


async def get_owned(
    session: AsyncSession,
    user_id: UUID,
    notification_id: UUID,
) -> Notification | None:
    return await session.scalar(
        select(Notification)
        .where(Notification.id == notification_id, Notification.user_id == user_id)
        .execution_options(populate_existing=True)
    )
