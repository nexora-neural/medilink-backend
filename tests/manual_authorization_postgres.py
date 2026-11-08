# ruff: noqa: E501, I001
import asyncio
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.modules.identity.authorization import (
    require_doctor_grant,
    require_patient_source_correction,
)
from app.modules.identity.models import User
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


async def main():
    engine = create_async_engine(os.environ["ML21_TEST_DATABASE_URL"])
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:  # noqa: SIM117
        async with session.begin():
            patient, doctor, org, grant, consultation = [uuid4() for _ in range(5)]
            now = datetime.now(UTC)
            await session.execute(
                text(
                    "INSERT INTO users (id,firebase_uid,display_name,status) VALUES (:p,:pu,'Patient','active'),(:d,:du,'Doctor','active')"
                ),
                {"p": patient, "pu": str(patient), "d": doctor, "du": str(doctor)},
            )
            await session.execute(
                text(
                    "INSERT INTO roles (code,label) VALUES ('patient','Patient'),('doctor','Doctor') ON CONFLICT (code) DO NOTHING"
                )
            )
            await session.execute(
                text(
                    "INSERT INTO user_roles (user_id,role_id) SELECT :u,id FROM roles WHERE code='doctor'"
                ),
                {"u": doctor},
            )
            await session.execute(
                text(
                    "INSERT INTO user_roles (user_id,role_id) SELECT :u,id FROM roles WHERE code='patient'"
                ),
                {"u": patient},
            )
            await session.execute(
                text(
                    "INSERT INTO organizations (id,name,type,status) VALUES (:id,'Clinic','clinic','active')"
                ),
                {"id": org},
            )
            await session.execute(
                text(
                    "INSERT INTO organization_memberships (id,organization_id,user_id,role,status) VALUES (:id,:org,:u,'doctor','approved')"
                ),
                {"id": uuid4(), "org": org, "u": doctor},
            )
            await session.execute(
                text(
                    "INSERT INTO professional_profiles (user_id,role,doctor_reference,verification_status) VALUES (:u,'doctor',:ref,'approved')"
                ),
                {"u": doctor, "ref": str(doctor)},
            )
            await session.execute(
                text(
                    "INSERT INTO patient_profiles (user_id,date_of_birth,sex) VALUES (:u,'2000-01-01','female')"
                ),
                {"u": patient},
            )
            await session.execute(
                text(
                    "INSERT INTO access_grants (id,patient_user_id,professional_user_id,organization_id,allow_consultation,status,starts_at,expires_at) VALUES (:id,:p,:d,:o,true,'active',:start,:end)"
                ),
                {
                    "id": grant,
                    "p": patient,
                    "d": doctor,
                    "o": org,
                    "start": now - timedelta(days=1),
                    "end": now + timedelta(days=1),
                },
            )
            await session.execute(
                text(
                    "INSERT INTO grant_categories (grant_id,category) VALUES (:id,'prescriptions')"
                ),
                {"id": grant},
            )
            d = await session.get(User, doctor)
            p = await session.get(User, patient)

            async def rejected(call):
                try:
                    await call
                except HTTPException as exc:
                    assert exc.status_code == 403
                    return
                raise AssertionError("Authorization unexpectedly allowed")

            await require_doctor_grant(session, d, patient, org, "prescriptions")
            await session.execute(
                text("UPDATE access_grants SET organization_id=NULL WHERE id=:id"),
                {"id": grant},
            )
            await rejected(require_doctor_grant(session, d, patient, org, "prescriptions"))
            await session.execute(
                text("UPDATE access_grants SET organization_id=:org WHERE id=:id"),
                {"id": grant, "org": org},
            )
            await rejected(require_doctor_grant(session, d, patient, org, "reports"))
            await rejected(
                require_doctor_grant(
                    session, d, patient, org, "prescriptions", clinical_write=True
                )
            )
            await session.execute(
                text(
                    "INSERT INTO consultations (id,patient_user_id,doctor_user_id,organization_id,grant_id) VALUES (:id,:p,:d,:o,:g)"
                ),
                {"id": consultation, "p": patient, "d": doctor, "o": org, "g": grant},
            )
            await require_doctor_grant(
                session, d, patient, org, "prescriptions", clinical_write=True
            )
            await session.execute(
                text("UPDATE consultations SET ended_at=now() WHERE id=:id"),
                {"id": consultation},
            )
            await rejected(
                require_doctor_grant(
                    session, d, patient, org, "prescriptions", clinical_write=True
                )
            )
            await session.execute(
                text(
                    "UPDATE access_grants SET expires_at=now()-interval '1 second' WHERE id=:id"
                ),
                {"id": grant},
            )
            await rejected(
                require_doctor_grant(session, d, patient, org, "prescriptions")
            )
            await session.execute(
                text(
                    "UPDATE access_grants SET expires_at=now()+interval '1 day',status='revoked',revoked_at=now() WHERE id=:id"
                ),
                {"id": grant},
            )
            await rejected(
                require_doctor_grant(session, d, patient, org, "prescriptions")
            )
            await session.execute(
                text(
                    "UPDATE access_grants SET status='active',revoked_at=NULL WHERE id=:id"
                ),
                {"id": grant},
            )
            await session.execute(
                text("UPDATE organizations SET status='suspended' WHERE id=:id"),
                {"id": org},
            )
            await rejected(
                require_doctor_grant(session, d, patient, org, "prescriptions")
            )
            await session.execute(
                text("UPDATE organizations SET status='active' WHERE id=:id"),
                {"id": org},
            )
            await session.execute(
                text(
                    "UPDATE organization_memberships SET status='suspended' WHERE user_id=:id"
                ),
                {"id": doctor},
            )
            await rejected(
                require_doctor_grant(session, d, patient, org, "prescriptions")
            )
            await session.execute(
                text(
                    "UPDATE organization_memberships SET status='approved' WHERE user_id=:id"
                ),
                {"id": doctor},
            )
            await session.execute(
                text(
                    "UPDATE professional_profiles SET verification_status='suspended' WHERE user_id=:id"
                ),
                {"id": doctor},
            )
            await rejected(
                require_doctor_grant(session, d, patient, org, "prescriptions")
            )
            await require_patient_source_correction(
                session,
                p,
                patient_id=patient,
                author_id=patient,
                source="patient",
                status="active",
            )
            await rejected(
                require_patient_source_correction(
                    session,
                    p,
                    patient_id=patient,
                    author_id=patient,
                    source="doctor",
                    status="active",
                )
            )
            await rejected(
                require_patient_source_correction(
                    session,
                    p,
                    patient_id=uuid4(),
                    author_id=patient,
                    source="patient",
                    status="active",
                )
            )
            await session.rollback()
    await engine.dispose()
    print(
        "PASS: grant, category, consultation, expiry, revocation, ownership and source"
    )


asyncio.run(main())
