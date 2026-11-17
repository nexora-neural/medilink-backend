"""ML-61 controlled bootstrap guards and optional PostgreSQL integration tests.

Set ML61_TEST_DATABASE_URL to a dedicated, fully migrated PostgreSQL test DB to
run the DB-backed cases. NEVER use a live/prod database for these tests.
"""

import asyncio
import os
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase, skipUnless
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.database.bootstrap_super_admin import (
    fetch_verified_firebase_account,
    run_bootstrap,
    validate_operator_config,
)
from app.modules.audit.models import AuditEvent
from app.modules.identity.bootstrap import BootstrapRejected, bootstrap_first_super_admin
from app.modules.identity.models import Role, User, UserRole

TEST_DATABASE_URL = os.getenv("ML61_TEST_DATABASE_URL")


class BootstrapOperatorConfigTests(TestCase):
    def test_requires_explicit_firebase_project(self):
        with (
            patch.dict(os.environ, {"APP_ENV": "test", "ML61_BOOTSTRAP_APPROVED_UID": "allowed"}),
            patch.object(settings, "firebase_project_id", None),
            self.assertRaises(BootstrapRejected),
        ):
            validate_operator_config("allowed")

    def test_requires_explicit_environment_and_approved_uid(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(BootstrapRejected):
                validate_operator_config("synthetic-uid")
        with (
            patch.dict(
                os.environ,
                {"APP_ENV": "test", "ML61_BOOTSTRAP_APPROVED_UID": "allowed"},
                clear=True,
            ),
            patch.object(settings, "firebase_project_id", "test-firebase-project"),
        ):
            with self.assertRaises(BootstrapRejected):
                validate_operator_config("different")
            validate_operator_config("allowed")

    def test_production_needs_explicit_change_approval_flag(self):
        environment = {"APP_ENV": "production", "ML61_BOOTSTRAP_APPROVED_UID": "approved"}
        with (
            patch.dict(os.environ, environment, clear=True),
            patch.object(settings, "firebase_project_id", "test-firebase-project"),
        ):
            with self.assertRaises(BootstrapRejected):
                validate_operator_config("approved")
            os.environ["ML61_BOOTSTRAP_PRODUCTION_APPROVAL"] = "APPROVED"
            validate_operator_config("approved")

    def test_requires_enabled_verified_firebase_account(self):
        record = SimpleNamespace(
            uid="synthetic-uid",
            email="synthetic@example.invalid",
            email_verified=True,
            disabled=False,
            display_name="Synthetic ML61 Admin",
        )
        with (
            patch("app.database.bootstrap_super_admin._firebase_app", return_value=object()),
            patch("app.database.bootstrap_super_admin.auth.get_user", return_value=record),
        ):
            self.assertEqual(
                fetch_verified_firebase_account("synthetic-uid"),
                ("synthetic@example.invalid", "Synthetic ML61 Admin"),
            )
            record.email_verified = False
            with self.assertRaises(BootstrapRejected):
                fetch_verified_firebase_account("synthetic-uid")
            record.email_verified = True
            record.disabled = True
            with self.assertRaises(BootstrapRejected):
                fetch_verified_firebase_account("synthetic-uid")

    def test_rejects_mismatched_uid_missing_email_and_invalid_name(self):
        valid = dict(
            uid="synthetic-uid",
            email="synthetic@example.invalid",
            email_verified=True,
            disabled=False,
            display_name="Synthetic Operator",
        )
        for change in (
            {"uid": "different"},
            {"email": None},
            {"display_name": "   "},
            {"display_name": "x" * 151},
        ):
            with (
                self.subTest(change=change),
                patch("app.database.bootstrap_super_admin._firebase_app", return_value=object()),
                patch(
                    "app.database.bootstrap_super_admin.auth.get_user",
                    return_value=SimpleNamespace(**(valid | change)),
                ),
                self.assertRaises(BootstrapRejected),
            ):
                fetch_verified_firebase_account("synthetic-uid")


@skipUnless(TEST_DATABASE_URL, "Set ML61_TEST_DATABASE_URL to a fresh migrated test database")
class BootstrapPostgreSQLTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine(TEST_DATABASE_URL)
        self.addAsyncCleanup(self.engine.dispose)
        self.connection = await self.engine.connect()
        self.addAsyncCleanup(self.connection.close)
        self.transaction = await self.connection.begin()
        self.addAsyncCleanup(self.transaction.rollback)
        self.session = AsyncSession(
            bind=self.connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        self.addAsyncCleanup(self.session.close)

        existing = await self.session.scalar(
            select(UserRole.user_id)
            .join(Role, UserRole.role_id == Role.id)
            .where(Role.code == "super_admin")
            .limit(1)
        )
        if existing is not None:
            self.skipTest("Use a fresh disposable database with no Super Admin assignment")
        await self.session.commit()
        self.uid = f"synthetic-ml61-{uuid4()}"
        self.email = f"ml61-{uuid4().hex}@example.invalid"
        self.request_id = f"ml61-test-{uuid4()}"

    async def perform_bootstrap(self):
        return await bootstrap_first_super_admin(
            self.session,
            firebase_uid=self.uid,
            verified_email=self.email,
            display_name="Synthetic Bootstrap Account",
            request_id=self.request_id,
        )

    async def test_creates_first_super_admin_and_append_only_audit(self):
        uid = await self.perform_bootstrap()
        await self.session.commit()
        local_user = await self.session.scalar(select(User).where(User.id == uid))
        assignment = await self.session.scalar(
            select(UserRole)
            .join(Role, Role.id == UserRole.role_id)
            .where(UserRole.user_id == uid, Role.code == "super_admin")
        )
        event = await self.session.scalar(
            select(AuditEvent).where(AuditEvent.request_id == self.request_id)
        )
        self.assertIsNotNone(local_user)
        self.assertEqual(local_user.email, self.email)
        self.assertIsNotNone(assignment)
        self.assertIsNone(assignment.assigned_by)
        self.assertIsNotNone(event)
        self.assertEqual(event.action, "account.super_admin.bootstrapped")
        self.assertEqual(event.outcome, "success")
        self.assertIsNone(event.actor_user_id)
        self.assertEqual(event.metadata_["actor_type"], "restricted_operator_cli")
        self.assertNotIn("email", event.metadata_)
        self.assertNotIn("firebase_uid", event.metadata_)

    async def test_duplicate_bootstrap_is_rejected(self):
        await self.perform_bootstrap()
        await self.session.commit()
        with self.assertRaises(BootstrapRejected):
            await self.perform_bootstrap()
        await self.session.rollback()
        active = await self.session.scalars(
            select(UserRole.user_id)
            .join(Role, Role.id == UserRole.role_id)
            .where(Role.code == "super_admin", UserRole.revoked_at.is_(None))
        )
        self.assertEqual(len(active.all()), 1)

    async def test_revoked_super_admin_assignment_still_blocks_rebootstrap(self):
        user_id = await self.perform_bootstrap()
        await self.session.commit()
        role_id = await self.session.scalar(select(Role.id).where(Role.code == "super_admin"))
        assignment = await self.session.get(UserRole, (user_id, role_id))
        assignment.revoked_at = datetime.now(UTC)
        await self.session.commit()
        self.uid = f"synthetic-ml61-{uuid4()}"
        self.email = f"ml61-{uuid4().hex}@example.invalid"
        self.request_id = f"ml61-test-{uuid4()}"
        with self.assertRaises(BootstrapRejected):
            await self.perform_bootstrap()
        await self.session.rollback()

    async def test_rejects_promotion_of_existing_patient_identity(self):
        role = await self.session.scalar(select(Role).where(Role.code == "patient"))
        if role is None:
            role = Role(code="patient", label="Patient")
            self.session.add(role)
            await self.session.flush()
        user = User(
            firebase_uid=self.uid,
            email=self.email,
            display_name="Synthetic Existing Patient",
            status="active",
        )
        self.session.add(user)
        await self.session.flush()
        self.session.add(UserRole(user_id=user.id, role_id=role.id))
        await self.session.commit()
        with self.assertRaises(BootstrapRejected):
            await self.perform_bootstrap()
        await self.session.rollback()
        event = await self.session.scalar(
            select(AuditEvent).where(AuditEvent.request_id == self.request_id)
        )
        self.assertIsNone(event)

    async def test_audit_failure_rolls_back_user_and_privilege(self):
        with (
            patch(
                "app.modules.identity.bootstrap.append_event",
                side_effect=RuntimeError("Synthetic audit failure"),
            ),
            self.assertRaises(RuntimeError),
        ):
            await self.perform_bootstrap()
        await self.session.rollback()
        self.assertIsNone(
            await self.session.scalar(select(User.id).where(User.firebase_uid == self.uid))
        )
        self.assertIsNone(
            await self.session.scalar(
                select(AuditEvent.id).where(AuditEvent.request_id == self.request_id)
            )
        )

    async def test_existing_roleless_active_identity_can_be_bootstrapped(self):
        user = User(
            firebase_uid=self.uid,
            email=self.email,
            display_name="Synthetic Dedicated Identity",
            status="active",
        )
        self.session.add(user)
        await self.session.flush()
        original_id = user.id
        self.assertEqual(await self.perform_bootstrap(), original_id)

    async def test_inactive_roleless_identity_is_rejected(self):
        self.session.add(
            User(
                firebase_uid=self.uid,
                email=self.email,
                display_name="Synthetic Suspended",
                status="suspended",
            )
        )
        await self.session.flush()
        with self.assertRaises(BootstrapRejected):
            await self.perform_bootstrap()

    async def test_email_mismatch_is_rejected(self):
        self.session.add(
            User(
                firebase_uid=self.uid,
                email="different@example.invalid",
                display_name="Synthetic Mismatch",
                status="active",
            )
        )
        await self.session.flush()
        with self.assertRaises(BootstrapRejected):
            await self.perform_bootstrap()

    async def test_no_role_is_persisted_after_failure(self):
        before = await self.session.scalar(select(func.count()).select_from(UserRole))
        with (
            patch(
                "app.modules.identity.bootstrap.append_event",
                side_effect=RuntimeError("Synthetic audit failure"),
            ),
            self.assertRaises(RuntimeError),
        ):
            await self.perform_bootstrap()
        await self.session.rollback()
        after = await self.session.scalar(select(func.count()).select_from(UserRole))
        self.assertEqual(before, after)


# This suite intentionally commits concurrent transactions. Use a DIFFERENT
# fresh migrated database from ML61_TEST_DATABASE_URL and all regression suites.
CONCURRENCY_URL = os.getenv("ML61_CONCURRENCY_DATABASE_URL")


@skipUnless(CONCURRENCY_URL, "Set ML61_CONCURRENCY_DATABASE_URL to a separate fresh test database")
class BootstrapConcurrencyTests(IsolatedAsyncioTestCase):
    async def test_concurrent_operators_commit_one_account_and_audit_denied_attempt(self):
        engine = create_async_engine(CONCURRENCY_URL)
        self.addAsyncCleanup(engine.dispose)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as session:
            previous = await session.scalar(
                select(UserRole.user_id)
                .join(Role, Role.id == UserRole.role_id)
                .where(Role.code == "super_admin")
                .limit(1)
            )
            if previous:
                self.fail("Use a fresh concurrency test database with no Super Admin assignment")

        ready = asyncio.Event()
        arrived = 0

        async def verified(_function, firebase_uid):
            nonlocal arrived
            arrived += 1
            if arrived == 2:
                ready.set()
            await ready.wait()
            return f"{firebase_uid}@example.invalid", "Synthetic Concurrent Operator"

        # Keep the real CLI transaction/denied-event path. Only operator approval
        # and external identity lookup are synthetic for these two test UIDs.
        async def attempt(firebase_uid):
            try:
                return "success", await run_bootstrap(firebase_uid)
            except BootstrapRejected:
                return "denied", None

        suffix = uuid4().hex
        with (
            patch("app.database.bootstrap_super_admin.async_session", sessions),
            patch("app.database.bootstrap_super_admin.validate_operator_config"),
            patch("app.database.bootstrap_super_admin.asyncio.to_thread", side_effect=verified),
        ):
            results = await asyncio.wait_for(
                asyncio.gather(
                    attempt(f"ml61-concurrent-a-{suffix}"), attempt(f"ml61-concurrent-b-{suffix}")
                ),
                timeout=15,
            )
        self.assertEqual(sorted(result[0] for result in results), ["denied", "success"])
        async with sessions() as session:
            assignments = await session.scalar(
                select(func.count())
                .select_from(UserRole)
                .join(Role, Role.id == UserRole.role_id)
                .where(Role.code == "super_admin")
            )
            self.assertEqual(assignments, 1)
            users = await session.scalar(
                select(func.count())
                .select_from(User)
                .where(
                    User.firebase_uid.in_(
                        [f"ml61-concurrent-a-{suffix}", f"ml61-concurrent-b-{suffix}"]
                    )
                )
            )
            self.assertEqual(users, 1)
            events = (
                await session.scalars(
                    select(AuditEvent).where(
                        AuditEvent.action.in_(
                            [
                                "account.super_admin.bootstrapped",
                                "account.super_admin.bootstrap_attempt",
                            ]
                        )
                    )
                )
            ).all()
            self.assertEqual(sorted(event.outcome for event in events), ["denied", "success"])
            for event in events:
                self.assertEqual(event.metadata_["source"], "offline_bootstrap")
                self.assertNotIn("firebase_uid", event.metadata_)
                self.assertNotIn("email", event.metadata_)
