"""Sign in to a Firebase test user and attach its role to the local database."""

import argparse
import asyncio
import getpass
import json
import os
import urllib.error
import urllib.request
from base64 import urlsafe_b64decode
from datetime import date
from uuid import uuid4

from sqlalchemy import select, text

from app.core.config import settings
from app.database.session import async_session, engine
from app.modules.identity.models import Role, User, UserRole
from app.modules.profiles.models import PatientProfile, ProfessionalProfile

ROLE_LABELS = {
    "patient": "Patient",
    "doctor": "Doctor",
    "pharmacist": "Pharmacist",
    "lab_staff": "Lab Staff",
    "admin": "Admin",
    "super_admin": "Super Admin",
}
PRIVILEGED_ROLES = {"admin", "super_admin"}
ALLOWED_ENVIRONMENTS = {"local", "development", "dev", "test"}


def firebase_identity(api_key: str, email: str, password: str) -> tuple[str, str]:
    url = (
        "https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key="
        + api_key
    )
    payload = json.dumps(
        {"email": email, "password": password, "returnSecureToken": True}
    ).encode()
    request = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Referer": "http://localhost:4200/",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            result = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        # Do not print Firebase's response body; it may echo account details.
        if exc.code == 400:
            raise ValueError("Firebase sign-in failed; check the email and password.") from None
        raise ValueError("Firebase sign-in service could not be reached.") from None
    except (urllib.error.URLError, TimeoutError):
        raise ValueError("Firebase sign-in service could not be reached.") from None

    token = result.get("idToken")
    uid = result.get("localId")
    returned_email = result.get("email")
    if not isinstance(token, str) or not isinstance(uid, str):
        raise ValueError("Firebase did not return a valid user identity.")
    if not isinstance(returned_email, str) or returned_email.casefold() != email.casefold():
        raise ValueError("Firebase returned an unexpected account email.")
    try:
        segment = token.split(".")[1]
        padding = "=" * (-len(segment) % 4)
        claims = json.loads(urlsafe_b64decode(segment + padding))
    except (IndexError, ValueError, json.JSONDecodeError):
        raise ValueError("Firebase returned an unreadable identity token.") from None
    if claims.get("aud") != settings.firebase_project_id or claims.get("user_id") != uid:
        raise ValueError("The Firebase account does not belong to the configured project.")
    return uid, returned_email


async def seed_account(role: str, uid: str, email: str, display_name: str) -> None:
    async with async_session.begin() as session:
        role_row = await session.scalar(select(Role).where(Role.code == role))
        if role_row is None:
            label = ROLE_LABELS[role]
            await session.execute(
                text(
                    "INSERT INTO roles (code, label) VALUES (:code, :label) "
                    "ON CONFLICT (code) DO NOTHING"
                ),
                {"code": role, "label": label},
            )
            role_row = await session.scalar(select(Role).where(Role.code == role))
        if role_row is None:
            raise ValueError("The requested role is not configured in the local database.")

        user = await session.scalar(select(User).where(User.firebase_uid == uid))
        if user is None:
            user = User(
                firebase_uid=uid,
                email=email,
                display_name=display_name,
                status="active",
            )
            session.add(user)
            await session.flush()
        elif user.email and user.email.casefold() != email.casefold():
            raise ValueError("This Firebase UID is already linked to a different email locally.")
        elif user.status != "active":
            raise ValueError("The existing local account is not active; no changes were made.")

        if await session.get(UserRole, (user.id, role_row.id)) is None:
            session.add(UserRole(user_id=user.id, role_id=role_row.id))

        if role == "patient":
            if await session.get(PatientProfile, user.id) is None:
                session.add(
                    PatientProfile(
                        user_id=user.id,
                        date_of_birth=date(1990, 1, 1),
                        sex="female",
                    )
                )
        elif role in {"doctor", "pharmacist", "lab_staff"}:
            profile = await session.get(ProfessionalProfile, user.id)
            if profile is None:
                session.add(
                    ProfessionalProfile(
                        user_id=user.id,
                        role=role,
                        doctor_reference=f"TEST-{uuid4().hex}" if role == "doctor" else None,
                    )
                )
            elif profile.role != role:
                raise ValueError("This local account already has a different professional role.")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", required=True, choices=ROLE_LABELS)
    parser.add_argument(
        "--email", required=True, help="Email of the existing Firebase test account"
    )
    parser.add_argument("--display-name", help="Optional synthetic display name")
    parser.add_argument(
        "--allow-privileged-role",
        action="store_true",
        help="Required when seeding Admin or Super Admin into a local/test database",
    )
    args = parser.parse_args()

    if os.getenv("APP_ENV", "local").lower() not in ALLOWED_ENVIRONMENTS:
        parser.error("Test accounts may only be seeded in local, development or test environments.")
    if args.role in PRIVILEGED_ROLES and not args.allow_privileged_role:
        parser.error("Admin roles require --allow-privileged-role.")
    api_key = os.getenv("FIREBASE_WEB_API_KEY", "").strip()
    if not api_key or not settings.firebase_project_id:
        parser.error("Set FIREBASE_WEB_API_KEY and FIREBASE_PROJECT_ID in the local .env.")

    password = getpass.getpass(f"Firebase password for {args.email}: ")
    try:
        uid, email = await asyncio.to_thread(firebase_identity, api_key, args.email, password)
        await seed_account(
            args.role,
            uid,
            email,
            args.display_name or f"MediLink Test {ROLE_LABELS[args.role]}",
        )
    except ValueError as exc:
        parser.error(str(exc))
    finally:
        password = ""
        await engine.dispose()
    print(f"Seeded {ROLE_LABELS[args.role]} local role for {email}.")


if __name__ == "__main__":
    asyncio.run(main())
