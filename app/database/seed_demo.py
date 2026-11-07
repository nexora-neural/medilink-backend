"""Insert repeatable synthetic role and verification examples for local development."""

import argparse
import asyncio
import os
from uuid import UUID, uuid5

from sqlalchemy import text

from app.database.session import engine

NAMESPACE = UUID("d4f5a904-3fd1-4f21-a2e6-f069cb9d94ba")
ROLES = (
    ("patient", "Patient"),
    ("doctor", "Doctor"),
    ("pharmacist", "Pharmacist"),
    ("lab_staff", "Lab Staff"),
    ("admin", "Admin"),
    ("super_admin", "Super Admin"),
)
USERS = (
    ("patient", "active"),
    ("doctor", "active"),
    ("pharmacist", "pending"),
    ("lab_staff", "suspended"),
    ("admin", "active"),
    ("super_admin", "active"),
)
VERIFICATIONS = (
    ("doctor", "approved"),
    ("pharmacist", "pending"),
    ("lab_staff", "suspended"),
)


def sample_id(kind: str, name: str) -> UUID:
    return uuid5(NAMESPACE, f"{kind}:{name}")


async def seed() -> None:
    async with engine.begin() as connection:
        for code, label in ROLES:
            await connection.execute(
                text(
                    "INSERT INTO roles (code, label) VALUES (:code, :label) "
                    "ON CONFLICT (code) DO NOTHING"
                ),
                {"code": code, "label": label},
            )
        for role, status in USERS:
            uid = f"synthetic:ml26:{role}"
            user_id = sample_id("user", role)
            await connection.execute(
                text(
                    "INSERT INTO users (id, firebase_uid, display_name, status) "
                    "VALUES (:id, :uid, :name, CAST(:status AS account_status)) "
                    "ON CONFLICT (firebase_uid) DO NOTHING"
                ),
                {
                    "id": user_id,
                    "uid": uid,
                    "name": f"Synthetic {role.replace('_', ' ').title()}",
                    "status": status,
                },
            )
            existing = await connection.scalar(
                text("SELECT id FROM users WHERE firebase_uid = :uid"), {"uid": uid}
            )
            if existing != user_id:
                raise RuntimeError(f"Synthetic identity collision for {role}; seed aborted")
            await connection.execute(
                text(
                    "INSERT INTO user_roles (user_id, role_id) "
                    "SELECT :user_id, id FROM roles WHERE code = :role "
                    "ON CONFLICT (user_id, role_id) DO NOTHING"
                ),
                {"user_id": user_id, "role": role},
            )
        for role, status in VERIFICATIONS:
            application_id = sample_id("verification", role)
            reviewed = status != "pending"
            await connection.execute(
                text(
                    "INSERT INTO verification_applications "
                    "(id, user_id, role, status, reviewed_by, reviewed_at, review_reason) "
                    "VALUES (:id, :user_id, CAST(:role AS professional_role), "
                    "CAST(:status AS verification_status), :reviewer, "
                    "CASE WHEN :reviewed THEN now() ELSE NULL END, :reason) "
                    "ON CONFLICT (id) DO NOTHING"
                ),
                {
                    "id": application_id,
                    "user_id": sample_id("user", role),
                    "role": role,
                    "status": status,
                    "reviewer": sample_id("user", "admin") if reviewed else None,
                    "reviewed": reviewed,
                    "reason": "Synthetic review example" if reviewed else None,
                },
            )
            if reviewed:
                await connection.execute(
                    text(
                        "INSERT INTO verification_decisions "
                        "(id, application_id, reviewer_user_id, decision, reason) "
                        "VALUES (:id, :application, :reviewer, :decision, :reason) "
                        "ON CONFLICT (id) DO NOTHING"
                    ),
                    {
                        "id": sample_id("decision", role),
                        "application": application_id,
                        "reviewer": sample_id("user", "admin"),
                        "decision": status,
                        "reason": "Synthetic review example",
                    },
                )


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Explicitly apply the synthetic seed")
    args = parser.parse_args()
    if not args.apply:
        parser.error("pass --apply to insert synthetic development records")
    if os.getenv("APP_ENV", "local").lower() not in {"local", "development", "dev", "test"}:
        parser.error("synthetic seeding is allowed only in local, development or test environments")
    try:
        await seed()
        print("Synthetic roles, users and verification examples seeded.")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
