"""Business rules and orchestration for prescriptions and OCR workflow."""



from fastapi import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.audit.models import AuditEvent
from app.modules.identity.auth import AuthenticationError
from app.modules.identity.models import User
from app.modules.identity.repository import get_roles
from app.modules.organizations.models import (
    Organization,
    OrganizationMembership,
)
from app.modules.prescriptions import repository
from app.modules.prescriptions.schemas import (
    MedicineInput,
    MedicinesRead,
    MedicinesReadPage,
)
from app.modules.profiles.models import ProfessionalProfile


def forbidden(request: Request) -> AuthenticationError:
    return AuthenticationError(
        status_code=403,
        detail={
            "code": "ACCESS_DENIED",
            "message": "Permission denied.",
            "field_errors": {},
            "request_id": request.state.request_id,
        },
    )


async def require_medicine_reader(
    session: AsyncSession,
    user: User,
    request: Request,
) -> None:
    """Allow active patients and verified active doctors."""

    if user.status != "active":
        raise forbidden(request)

    roles = await get_roles(session, user.id)

    if "patient" in roles:
        return

    if "doctor" not in roles:
        raise forbidden(request)

    professional = await session.get(
        ProfessionalProfile, user.id
    )

    if (
        professional is None
        or professional.role != "doctor"
        or professional.verification_status != "approved"
    ):
        raise forbidden(request)

    statement = (
        select(OrganizationMembership.id)
        .join(
            Organization,
            Organization.id == OrganizationMembership.organization_id,
        )
        .where(
            OrganizationMembership.user_id == user.id,
            OrganizationMembership.role == "doctor",
            OrganizationMembership.status == "approved",
            Organization.status == "active",
        )
        .limit(1)
    )

    membership_id = await session.scalar(statement)

    if membership_id is None:
        raise forbidden(request)


async def require_medicine_admin(
    session: AsyncSession,
    user: User,
    request: Request,
) -> None:
    """Check privileged catalogue management permission."""

    if user.status != "active":
        raise forbidden(request)

    roles = await get_roles(session, user.id)

    # Super Admin is the platform's privileged managing role.
    if "super_admin" in roles:
        return

    # A regular Admin requires an explicit delegated permission.
    # The existing repository does not yet implement the
    # delegated-permission policy/check.
    #
    # Deny regular Admin writes until that shared check exists.
    # Never grant catalogue management merely from a role
    # selected during public onboarding.
    raise forbidden(request)


async def list_medicines(
    session: AsyncSession,
    user: User,
    request: Request,
    q: str | None,
    page: int,
    page_size: int,
) -> MedicinesReadPage:
    """Return searchable sample catalogue entries."""

    await require_medicine_reader(
        session=session,
        user=user,
        request=request,
    )

    medicines, total = await repository.get_medicines(
        session=session,
        q=q,
        page=page,
        page_size=page_size,
    )

    return MedicinesReadPage(
        items=[
            MedicinesRead.model_validate(medicine)
            for medicine in medicines
        ],
        page=page,
        page_size=page_size,
        total=total,
    )


async def add_medicine(
    session: AsyncSession,
    user: User,
    request: Request,
    payload: MedicineInput,
) -> MedicinesRead:
    """Create a medicine and append its audit event atomically."""

    await require_medicine_admin(
        session=session,
        user=user,
        request=request,
    )

    try:
        medicine = await repository.create_medicine(
            session=session,
            payload=payload,
        )

        event = AuditEvent(
            actor_user_id=user.id,
            action="medicine.created",
            target_type="medicines",
            target_id=medicine.id,
            outcome="success",
            request_id=request.state.request_id,
            metadata_={
                "is_sample": True,
                "generic_name": medicine.generic_name,
            },
        )

        session.add(event)
        await session.flush()

        result = MedicinesRead.model_validate(medicine)

        await session.commit()
        return result

    except Exception:
        await session.rollback()
        raise


