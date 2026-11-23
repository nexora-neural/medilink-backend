"""ML-67 PostgreSQL tests in a private schema.

Set ML67_TEST_DATABASE_URL to a PostgreSQL+asyncpg URL whose user
may create schemas.

Each test uses a private schema inside an outer transaction.
Cleanup rolls back that transaction, including the schema.
Existing application schemas and rows are not modified.
"""

import os
import unittest
from datetime import UTC, date, datetime
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import (
    CheckConstraint,
    DefaultClause,
    MetaData,
    func,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import ENUM
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.database import models as _models  # noqa: F401
from app.database.base import Base
from app.modules.audit.models import AuditEvent, BackupRun, JobRun
from app.modules.audit.service import append_event
from app.modules.identity.models import Role, User, UserRole
from app.modules.measurements.models import Measurement
from app.modules.operations import service
from app.modules.operations.models import JobRetryRequest
from app.modules.profiles.models import PatientProfile

URL = os.getenv("ML67_TEST_DATABASE_URL")


def create_test_tables(connection):
    """Create isolated test tables without changing application metadata."""
    test_metadata = MetaData(
        naming_convention=Base.metadata.naming_convention,
    )

    for table in Base.metadata.sorted_tables:
        table.to_metadata(test_metadata)

    enum_names = (
        "schedule_status",
        "dose_status",
        "dose_response",
    )

    def local_enum_casts(expression):
        for enum_name in enum_names:
            expression = expression.replace(
                f"::public.{enum_name}",
                f"::{enum_name}",
            )

        return expression

    # Change only copied test definitions, not application metadata.
    for table in test_metadata.sorted_tables:
        for column in table.columns:
            # Identity columns also have a server_default, but no .arg.
            if isinstance(column.server_default, DefaultClause):
                original = str(column.server_default.arg)
                corrected = local_enum_casts(original)

                if corrected != original:
                    column.server_default = DefaultClause(
                        text(corrected)
                    )

        for constraint in table.constraints:
            if isinstance(constraint, CheckConstraint):
                original = str(constraint.sqltext)
                corrected = local_enum_casts(original)

                if corrected != original:
                    constraint.sqltext = text(corrected)

    # These enums are normally created by migrations.
    # Explicitly create them inside the private test schema.
    for table in test_metadata.sorted_tables:
        for column in table.columns:
            enum_type = column.type

            if isinstance(enum_type, ENUM) and not enum_type.create_type:
                enum_type.create(
                    connection,
                    checkfirst=True,
                )

    test_metadata.create_all(connection)


@unittest.skipUnless(
    URL,
    "Set ML67_TEST_DATABASE_URL for PostgreSQL checks",
)
class OperationsPostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine(
            URL,
            echo=False,
            hide_parameters=True,
        )
        self.addAsyncCleanup(self.engine.dispose)

        self.connection = await self.engine.connect()
        self.addAsyncCleanup(self.connection.close)

        self.outer = await self.connection.begin()
        self.addAsyncCleanup(self.rollback_test_transaction)

        schema = "ml67_test_" + uuid4().hex

        # This identifier is generated locally, not supplied by a user.
        await self.connection.execute(
            text(f'CREATE SCHEMA "{schema}"')
        )
        await self.connection.execute(
            text(f'SET LOCAL search_path TO "{schema}"')
        )

        await self.connection.run_sync(create_test_tables)

        self.db = AsyncSession(
            self.connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        self.addAsyncCleanup(self.db.close)

        self.admin = await self.user("admin")
        self.super_admin = await self.user("super_admin")
        self.patient = await self.user("patient")
        self.other = await self.user("patient")

        for user in (self.patient, self.other):
            self.db.add(
                PatientProfile(
                    user_id=user.id,
                    date_of_birth=date(2000, 1, 1),
                    sex="male",
                )
            )

        await self.db.flush()
        await self.db.commit()

    async def rollback_test_transaction(self):
        if self.outer.is_active:
            await self.outer.rollback()

    async def user(self, role_code):
        role = await self.db.scalar(
            select(Role).where(Role.code == role_code)
        )

        if role is None:
            role = Role(
                code=role_code,
                label=role_code,
            )
            self.db.add(role)
            await self.db.flush()

        user = User(
            firebase_uid=str(uuid4()),
            display_name="Synthetic test",
            status="active",
        )
        self.db.add(user)
        await self.db.flush()

        self.db.add(
            UserRole(
                user_id=user.id,
                role_id=role.id,
            )
        )
        await self.db.flush()

        return user

    async def job(self, attempts=1):
        row = JobRun(
            job_key=str(uuid4()),
            job_type="operations.database_check",
            status="failed",
            attempt_count=attempts,
            last_error_code="JOB_FAILED",
        )
        self.db.add(row)
        await self.db.flush()

        return row

    async def event(
        self,
        action,
        kind="job_run",
        target_id=None,
        actor=None,
    ):
        return await append_event(
            self.db,
            action=action,
            target_type=kind,
            target_id=target_id,
            actor_user_id=(actor or self.admin).id,
            outcome="success",
            request_id=str(uuid4()),
        )

    async def test_patient_activity_includes_other_authors_but_excludes_other_patients(
        self,
    ):
        for patient in (self.patient, self.other):
            row = Measurement(
                patient_user_id=patient.id,
                type="weight",
                value=60,
                unit="kg",
                measured_at=datetime.now(UTC),
                recorded_by=self.admin.id,
                source="doctor",
            )
            self.db.add(row)
            await self.db.flush()

            await self.event(
                "measurement.created",
                "measurement",
                row.id,
            )

        # A privileged event must not become patient activity merely
        # because the patient is its actor.
        await self.event(
            "account.super_admin.bootstrapped",
            "users",
            self.patient.id,
            self.patient,
        )

        result = await service.activity(
            self.db,
            self.patient,
            self.patient.id,
            1,
            20,
        )

        self.assertEqual(result.total, 1)
        self.assertEqual(
            result.items[0].action,
            "measurement.created",
        )
        self.assertNotIn(
            "actor_user_id",
            result.items[0].model_dump(),
        )

        with self.assertRaises(HTTPException) as ctx:
            await service.activity(
                self.db,
                self.patient,
                self.other.id,
                1,
                20,
            )

        self.assertEqual(ctx.exception.status_code, 404)

    async def test_admin_filter_cannot_widen_privileged_audit_visibility(self):
        await self.event("job.retry.requested")
        await self.event(
            "account.super_admin.bootstrapped",
            "users",
            self.super_admin.id,
        )

        admin = await service.audit(
            self.db,
            self.admin,
            1,
            20,
            None,
            None,
            None,
            None,
        )
        self.assertEqual(admin.total, 1)

        filtered = await service.audit(
            self.db,
            self.admin,
            1,
            20,
            "account.super_admin.bootstrapped",
            None,
            None,
            None,
        )
        self.assertEqual(filtered.total, 0)

        privileged = await service.audit(
            self.db,
            self.super_admin,
            1,
            20,
            None,
            None,
            None,
            None,
        )
        self.assertEqual(privileged.total, 2)

    async def test_retry_replay_worker_and_retained_history(self):
        job = await self.job()

        one = await service.retry(
            self.db,
            self.admin,
            job.id,
            "Synthetic recovery reason",
            str(uuid4()),
        )
        two = await service.retry(
            self.db,
            self.admin,
            job.id,
            "Repeat submission",
            str(uuid4()),
        )

        self.assertEqual(one.status, "pending")
        self.assertEqual(two.id, one.id)
        self.assertEqual(
            await self.db.scalar(
                select(func.count()).select_from(JobRetryRequest)
            ),
            1,
        )

        self.assertTrue(await service.worker_once(self.db))

        await self.db.refresh(job)

        self.assertEqual(job.status, "succeeded")
        self.assertEqual(job.attempt_count, 2)

        item = await self.db.scalar(select(JobRetryRequest))

        self.assertEqual(
            item.reason,
            "Synthetic recovery reason",
        )
        self.assertEqual(item.status, "succeeded")
        self.assertEqual(
            await self.db.scalar(
                select(func.count()).select_from(AuditEvent)
            ),
            2,
        )

    async def test_worker_rechecks_revoked_admin_role(self):
        job = await self.job()

        await service.retry(
            self.db,
            self.admin,
            job.id,
            "Recovery",
            str(uuid4()),
        )

        role = await self.db.scalar(
            select(Role).where(Role.code == "admin")
        )
        assignment = await self.db.get(
            UserRole,
            (self.admin.id, role.id),
        )

        assignment.revoked_at = datetime.now(UTC)
        await self.db.commit()

        await service.worker_once(self.db)
        await self.db.refresh(job)

        self.assertEqual(job.attempt_count, 1)
        self.assertEqual(job.status, "failed")

        item = await self.db.scalar(select(JobRetryRequest))

        self.assertEqual(item.status, "cancelled")

    async def test_retry_bound_and_inactive_admin_are_denied(self):
        job = await self.job(attempts=3)

        with self.assertRaises(HTTPException) as ctx:
            await service.retry(
                self.db,
                self.admin,
                job.id,
                "Recovery",
                str(uuid4()),
            )

        self.assertEqual(ctx.exception.status_code, 409)

        await self.db.refresh(self.admin)

        self.admin.status = "suspended"
        await self.db.commit()

        with self.assertRaises(HTTPException) as ctx:
            await service.summary(
                self.db,
                self.admin,
            )

        self.assertEqual(ctx.exception.status_code, 403)

    async def test_backup_evidence_and_successful_complete_backup_timestamp(self):
        now = datetime.now(UTC)

        self.db.add(
            BackupRun(
                status="succeeded",
                database_included=True,
                files_included=True,
                started_at=now,
                finished_at=now,
                restore_check_status="passed",
                restore_checked_at=now,
            )
        )
        self.db.add(
            BackupRun(
                status="failed",
                database_included=False,
                files_included=False,
                started_at=now,
                error_code="secret stack trace",
            )
        )
        await self.db.flush()

        summary = await service.summary(
            self.db,
            self.admin,
        )
        self.assertEqual(summary.last_backup_at, now)

        result = await service.backups(
            self.db,
            self.admin,
            1,
            20,
        )

        self.assertEqual(result.total, 2)
        self.assertNotIn(
            "secret stack trace",
            result.model_dump_json(),
        )
        self.assertTrue(
            any(
                item.restore_check_status == "passed"
                for item in result.items
            )
        )