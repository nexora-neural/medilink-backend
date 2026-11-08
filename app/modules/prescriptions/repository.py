"""Database access methods for prescriptions and OCR workflow."""


from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.prescriptions.core_models import Medicine
from app.modules.prescriptions.schemas import MedicineInput


async def get_medicines(
    session: AsyncSession,
    q: str | None,
    page: int,
    page_size: int,
) -> tuple[list[Medicine], int]:
    """Search and paginate sample medicines."""

    statement = select(Medicine).where(
        Medicine.is_sample.is_(True)
    )

    if q and q.strip():
        # Escape user-provided SQL LIKE wildcard characters.
        term = (
            q.strip()
            .replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
        )

        pattern = f"%{term}%"

        statement = statement.where(
            or_(
                Medicine.generic_name.ilike(
                    pattern, escape="\\"
                ),
                Medicine.brand_name.ilike(
                    pattern, escape="\\"
                ),
                Medicine.strength.ilike(
                    pattern, escape="\\"
                ),
                Medicine.form.ilike(
                    pattern, escape="\\"
                ),
            )
        )

    count_query = select(func.count()).select_from(
        statement.order_by(None).subquery()
    )

    total = (await session.scalar(count_query)) or 0

    statement = (
        statement
        .order_by(
            Medicine.created_at.desc(),
            Medicine.id.desc(),
        )
        .offset((page - 1) * page_size)
        .limit(page_size)
    )

    result = await session.scalars(statement)

    return list(result.all()), total


async def create_medicine(
    session: AsyncSession,
    payload: MedicineInput,
) -> Medicine:
    """Insert a sample medicine without committing."""

    medicine = Medicine(
        generic_name=payload.generic_name,
        brand_name=payload.brand_name,
        strength=payload.strength,
        form=payload.form,
        is_sample=True,
    )

    session.add(medicine)
    await session.flush()
    await session.refresh(medicine)

    return medicine

