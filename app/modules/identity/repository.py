"""Database access for local roles and account support."""

from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.identity.enums import AccountStatus
from app.modules.identity.models import Role, User, UserRole


async def get_roles(session: AsyncSession, user_id: UUID) -> set[str]:
    """Return the account's current role codes."""
    roles = await session.scalars(
        select(Role.code)
        .join(UserRole, UserRole.role_id == Role.id)
        .where(UserRole.user_id == user_id, UserRole.revoked_at.is_(None))
    )
    return set(roles.all())


async def find_accounts(
    session: AsyncSession,
    *,
    query: str | None,
    status: AccountStatus | None,
    page: int,
    page_size: int,
) -> tuple[list[User], int]:
    """Search operational identity fields and return a deterministic page."""
    filters = []
    if status is not None:
        filters.append(User.status == status)
    if query:
        matches = [
            func.lower(User.email) == query.lower(),
            User.phone == query,
            User.display_name.istartswith(query, autoescape=True),
        ]
        try:
            user_id = UUID(query)
        except ValueError:
            pass
        else:
            matches.append(User.id == user_id)
        filters.append(or_(*matches))

    total = await session.scalar(select(func.count()).select_from(User).where(*filters))
    offset = (page - 1) * page_size
    if offset >= total:
        return [], total
    users = await session.scalars(
        select(User)
        .where(*filters)
        .order_by(User.created_at.desc(), User.id.desc())
        .offset(offset)
        .limit(page_size)
    )
    return list(users.all()), total


async def count_active_super_admins(session: AsyncSession) -> int:
    """Count the active Super Admins within the account-support transaction."""
    return await session.scalar(
        select(func.count())
        .select_from(User)
        .join(UserRole, UserRole.user_id == User.id)
        .join(Role, Role.id == UserRole.role_id)
        .where(
            User.status == AccountStatus.ACTIVE,
            UserRole.revoked_at.is_(None),
            Role.code == "super_admin",
        )
    )


async def get_admin_user_ids(
    session: AsyncSession,
    user_ids: list[UUID],
) -> set[UUID]:
    """Return users with a current, non-revoked Admin assignment."""
    if not user_ids:
        return set()

    result = await session.scalars(
        select(UserRole.user_id)
        .join(Role, Role.id == UserRole.role_id)
        .where(
            UserRole.user_id.in_(user_ids),
            UserRole.revoked_at.is_(None),
            Role.code == "admin",
        )
    )
    return set(result.all())
