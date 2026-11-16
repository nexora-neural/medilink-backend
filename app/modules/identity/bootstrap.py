"""Controlled, single-use Super Admin bootstrap; no public API entrypoint.

This module deliberately does not implement, weaken, or bypass the existing
privileged-MFA checks. Bootstrap is an operator-only exception performed before
there is a local Super Admin, not a normal privileged-account management path.
"""

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.audit.service import append_event
from app.modules.identity.models import Role, User, UserRole


class BootstrapRejected(Exception):
    """Expected safe rejection of a controlled first-account bootstrap."""


async def bootstrap_first_super_admin(
    session: AsyncSession,
    *,
    firebase_uid: str,
    verified_email: str,
    display_name: str,
    request_id: str,
) -> UUID:
    """Provision the *first* Super Admin from a previously verified Firebase UID.

    The caller MUST verify the UID with the Firebase Admin SDK, obtain separate
    operator authorization and own the database transaction. This function never
    commits; the role assignment and audit must commit/rollback together.
    """
    if (
        not firebase_uid.strip()
        or not verified_email.strip()
        or not display_name.strip()
        or not request_id.strip()
    ):
        raise BootstrapRejected("A verified identity and request ID are required.")

    # Serialize all supported invocations, including simultaneous operators.
    # This same PostgreSQL advisory-lock namespace is reserved for ML-61.
    await session.execute(select(func.pg_advisory_xact_lock(717, 61)))

    # Never rebootstrap when a Super Admin assignment exists, *including revoked*
    # assignments. Recovery must be an explicitly reviewed separate procedure.
    prior_assignment = await session.scalar(
        select(UserRole.user_id)
        .join(Role, Role.id == UserRole.role_id)
        .where(Role.code == "super_admin")
        .limit(1)
    )
    if prior_assignment is not None:
        raise BootstrapRejected("Bootstrap is already consumed; use the recovery process.")

    user = await session.scalar(
        select(User).where(User.firebase_uid == firebase_uid).with_for_update()
    )
    canonical_email = verified_email.strip()
    if user is None:
        user = User(
            firebase_uid=firebase_uid,
            email=canonical_email,
            display_name=display_name.strip(),
            status="active",
        )
        session.add(user)
        await session.flush()
    else:
        if user.status != "active":
            raise BootstrapRejected("The selected local account is not active.")
        if not user.email or user.email.casefold() != canonical_email.casefold():
            raise BootstrapRejected("The Firebase and local account emails do not match.")
        # Require a dedicated privileged identity, not an account already used
        # as a patient, professional or delegated Admin (including revoked roles).
        prior_user_role = await session.scalar(
            select(UserRole.role_id).where(UserRole.user_id == user.id).limit(1)
        )
        if prior_user_role is not None:
            raise BootstrapRejected("Bootstrap requires an account with no existing roles.")

    role = await session.scalar(select(Role).where(Role.code == "super_admin"))
    if role is None:
        role = Role(code="super_admin", label="Super Admin")
        session.add(role)
        await session.flush()

    session.add(UserRole(user_id=user.id, role_id=role.id, assigned_by=None))
    await session.flush()

    # Validated, append-only business evidence. Do not put UID, email,
    # credentials, approval secrets or unstructured operator notes in metadata.
    await append_event(
        session,
        actor_user_id=None,
        action="account.super_admin.bootstrapped",
        target_type="users",
        target_id=user.id,
        outcome="success",
        request_id=request_id,
        metadata={
            "actor_type": "restricted_operator_cli",
            "source": "offline_bootstrap",
            "role": "super_admin",
        },
    )
    return user.id
