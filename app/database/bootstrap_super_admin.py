"""Offline, operator-restricted initial Super Admin setup (ML-61).

Run with ``python -m app.database.bootstrap_super_admin`` only from an
approved host/operator session. Not imported or mounted as an HTTP route.
"""

import argparse
import asyncio
import os
from uuid import uuid4

from firebase_admin import auth, exceptions
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import settings
from app.database.session import async_session, engine
from app.modules.audit.service import append_event
from app.modules.identity.auth import _firebase_app
from app.modules.identity.bootstrap import BootstrapRejected, bootstrap_first_super_admin

SUPPORTED_ENVS = frozenset({"local", "dev", "development", "test", "staging", "production"})


def validate_operator_config(firebase_uid: str) -> None:
    """Enforce explicit operational gates; OS/IAM access controls are also required."""
    env = os.getenv("APP_ENV", "").strip().lower()
    if env not in SUPPORTED_ENVS:
        raise BootstrapRejected("APP_ENV must explicitly name a supported environment.")

    allowed_uid = os.getenv("ML61_BOOTSTRAP_APPROVED_UID", "").strip()
    if not allowed_uid or firebase_uid != allowed_uid:
        raise BootstrapRejected("The UID is not approved for initial bootstrap.")

    if env == "production" and os.getenv("ML61_BOOTSTRAP_PRODUCTION_APPROVAL") != "APPROVED":
        raise BootstrapRejected("Production bootstrap requires an approved change window.")

    if not settings.firebase_project_id:
        raise BootstrapRejected("FIREBASE_PROJECT_ID must be explicitly configured.")


def fetch_verified_firebase_account(firebase_uid: str) -> tuple[str, str]:
    """Trust only Firebase Admin SDK lookups, never caller-provided email/role."""
    record = auth.get_user(firebase_uid, app=_firebase_app())
    if record.uid != firebase_uid or record.disabled:
        raise BootstrapRejected("Firebase account is unavailable for bootstrap.")
    if not record.email or not record.email_verified:
        raise BootstrapRejected("A Firebase account with verified email is required.")
    display_name = (record.display_name or "MediLink Super Admin").strip()
    if not display_name or len(display_name) > 150:
        raise BootstrapRejected("The Firebase display name is invalid.")
    return record.email, display_name


async def run_bootstrap(firebase_uid: str) -> str:
    validate_operator_config(firebase_uid)
    # Check the external identity before the database transaction.
    email, display_name = await asyncio.to_thread(fetch_verified_firebase_account, firebase_uid)
    request_id = f"ml61-bootstrap-{uuid4()}"
    try:
        async with async_session() as session:
            async with session.begin():
                await bootstrap_first_super_admin(
                    session,
                    firebase_uid=firebase_uid,
                    verified_email=email,
                    display_name=display_name,
                    request_id=request_id,
                )
    except BootstrapRejected:
        # Record a denied operator attempt separately after the failed business
        # transaction has been rolled back. Never include rejected UID or reason.
        async with async_session() as session:
            async with session.begin():
                await append_event(
                    session,
                    action="account.super_admin.bootstrap_attempt",
                    target_type="users",
                    outcome="denied",
                    request_id=request_id,
                    metadata={
                        "actor_type": "restricted_operator_cli",
                        "source": "offline_bootstrap",
                        "role": "super_admin",
                        "code": "BOOTSTRAP_REJECTED",
                    },
                )
        raise
    return request_id


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--firebase-uid", required=True, help="Pre-approved Firebase UID")
    parser.add_argument(
        "--confirm-initial-bootstrap",
        action="store_true",
        help="Explicitly acknowledge this is the first Super Admin in this database",
    )
    args = parser.parse_args()
    if not args.confirm_initial_bootstrap:
        parser.error("Pass --confirm-initial-bootstrap after obtaining operator approval.")

    try:
        request_id = await run_bootstrap(args.firebase_uid)
    except BootstrapRejected as exc:
        parser.exit(1, f"Bootstrap rejected: {exc}\n")
    except (exceptions.FirebaseError, SQLAlchemyError, ValueError):
        # Never echo underlying Firebase/SQL exception text, tokens or DB URLs.
        parser.exit(1, "Bootstrap unavailable; check restricted operational logs.\n")
    finally:
        await engine.dispose()
    print(f"Initial Super Admin bootstrap completed. Audit request ID: {request_id}")


if __name__ == "__main__":
    asyncio.run(main())
