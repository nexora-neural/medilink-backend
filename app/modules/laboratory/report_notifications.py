"""Transactional patient notifications shared by manual report publication flows."""

from uuid import UUID

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.notifications.models import Notification


async def notify_report_accepted(
    db: AsyncSession, *, patient_id: UUID, report_id: UUID,
) -> None:
    """Enqueue once per published version; the caller owns commit and rollback."""
    await db.execute(
        insert(Notification).values(
            user_id=patient_id,
            notification_type="report_accepted",
            title="Laboratory report available",
            body="A laboratory report has been added to your records.",
            related_type="report",
            related_id=report_id,
            dedupe_key=f"report_accepted:{report_id}",
        ).on_conflict_do_nothing(index_elements=[Notification.dedupe_key])
    )