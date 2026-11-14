import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest import TestCase
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import app
from app.modules.identity import auth as identity_auth
from app.modules.measurements import router, service

PATIENT = uuid4()
ACTOR = uuid4()
READING = uuid4()
CONSULTATION = uuid4()
ROOT = f"{settings.api_v1_prefix}/patients/{PATIENT}/measurements"
CORRECTION = f"{settings.api_v1_prefix}/measurements/{READING}/corrections"
AUTH = {"Authorization": "Bearer synthetic-token"}
KEY = AUTH | {"Idempotency-Key": "review-test-001"}
TIME = "2025-01-01T09:00:00+05:30"
BP = {
    "type": "blood_pressure",
    "value": 120,
    "secondary_value": 80,
    "unit": "mmHg",
    "measured_at": TIME,
}
FIX = {k: v for k, v in BP.items() if k != "type"} | {"reason": "Entry typo", "value": 118}


class Session:
    @asynccontextmanager
    async def begin(self):
        yield

    async def execute(self, statement, params):
        if "INSERT INTO api_idempotency_records" in str(statement):
            self.replay[(params["actor"], params["target"], params["key"])] = {
                "request_digest": params["digest"],
                "response_body": json.loads(params["body"]),
            }


def setup_client(test):
    session = Session()
    session.replay = {}
    test.enterContext(patch.dict(app.dependency_overrides, {router.get_session: lambda: session}))
    return TestClient(app)


def setup_permissions(test):
    state = {"doctor": False, "grant": True, "consultation": True, "account": "active"}

    def verify(token, *, app, check_revoked):
        assert token == "synthetic-token"
        return {"uid": "patient-or-doctor"}

    async def actor_lookup(db, statement, **params):
        return {
            "id": ACTOR if state["doctor"] else PATIENT,
            "status": state["account"],
            "is_patient": not state["doctor"],
            "is_doctor": state["doctor"],
            "is_verified_doctor": state["doctor"],
        }

    async def permissions_lookup(db, statement, **params):
        if "FROM patient_profiles" in statement:
            return {"status": "active"}
        if "FROM consultations" in statement:
            assert params["consultation"] == CONSULTATION
            assert "c.ended_at IS NULL" in statement
            assert "g.allow_consultation" in statement
            return {"id": CONSULTATION} if state["consultation"] else None
        if "FROM access_grants" in statement:
            assert "measurements_vaccinations" in statement
            return {"id": uuid4()} if state["grant"] else None
        if "FROM api_idempotency_records" in statement:
            return db.replay.get((params["actor"], params["target"], params["key"]))
        raise AssertionError(statement)

    test.enterContext(patch.object(identity_auth, "_firebase_app", object))
    test.enterContext(patch.object(identity_auth.auth, "verify_id_token", verify))
    test.enterContext(patch.object(router, "fetch_one", actor_lookup))
    test.enterContext(patch.object(service, "fetch_one", permissions_lookup))
    return state


def setup_writes(test):
    events = []
    previous = {
        "patient_user_id": PATIENT,
        "type": "blood_pressure",
        "status": "active",
        "source": "patient",
    }

    async def insert(db, body, patient, actor, kind, source, previous=None, reason=None):
        return {
            "id": uuid4(),
            "patient_user_id": patient,
            "type": kind,
            "value": body.value,
            "secondary_value": body.secondary_value,
            "unit": body.unit,
            "measured_at": body.measured_at,
            "recorded_by": actor,
            "source": source,
            "notes": body.notes,
            "status": "active",
            "supersedes_id": previous,
            "correction_reason": reason,
            "created_at": datetime.now(UTC),
        }

    async def lock(db, measurement_id, patient):
        return previous if measurement_id == READING and patient == PATIENT else None

    async def supersede(db, measurement_id):
        assert measurement_id == READING
        previous["status"] = "superseded"

    async def audit(db, actor, action, reading, request_id, metadata):
        events.append((action, metadata))

    test.enterContext(patch.object(service, "insert_reading", insert))
    test.enterContext(patch.object(service, "lock_owned_reading", lock))
    test.enterContext(patch.object(service, "mark_superseded", supersede))
    test.enterContext(patch.object(service, "audit", audit))
    return events, previous


class MeasurementsReviewTests(TestCase):
    def test_active_actor_sets_request_audit_identity_before_authorization(self):
        from types import SimpleNamespace

        request = SimpleNamespace(state=SimpleNamespace())
        actor = {"id": ACTOR, "status": "active"}

        async def fetch(*args, **kwargs):
            return actor

        with patch.object(router, "fetch_one", fetch):
            import asyncio
            result = asyncio.run(router.active_actor(object(), "firebase-uid", request))

        self.assertEqual(result["id"], ACTOR)
        self.assertEqual(request.state.audit_actor_user_id, ACTOR)

    def test_denied_inactive_measurement_actor_is_still_auditable(self):
        from types import SimpleNamespace

        request = SimpleNamespace(state=SimpleNamespace())
        actor = {"id": ACTOR, "status": "suspended"}

        async def fetch(*args, **kwargs):
            return actor

        with patch.object(router, "fetch_one", fetch):
            import asyncio
            with self.assertRaises(router.MeasurementError):
                asyncio.run(router.active_actor(object(), "firebase-uid", request))

        self.assertEqual(request.state.audit_actor_user_id, ACTOR)

    def test_correction_browser_preflight(self):
        http = setup_client(self)
        origin = settings.cors_origins[0]
        response = http.options(
            CORRECTION,
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization,content-type,idempotency-key",
            },
        )
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == origin
        assert "idempotency-key" in response.headers["access-control-allow-headers"].lower()

    def test_missing_token(self):
        http = setup_client(self)
        response = http.get(ROOT)
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"

    def test_first_token_uses_lazy_firebase_initializer(self):
        http = setup_client(self)
        app_instance = object()
        state = {}

        def get_app():
            if "app" not in state:
                raise ValueError("App not initialized")
            return state["app"]

        def init_app(*, options):
            state["app"] = app_instance
            return app_instance

        def verify(token, *, app, check_revoked):
            assert app is app_instance and check_revoked
            raise identity_auth.auth.InvalidIdTokenError("Synthetic invalid token")

        self.enterContext(patch.object(identity_auth.firebase_admin, "get_app", get_app))
        self.enterContext(patch.object(identity_auth.firebase_admin, "initialize_app", init_app))
        self.enterContext(patch.object(identity_auth.auth, "verify_id_token", verify))
        identity_auth._firebase_app.cache_clear()
        try:
            response = http.get(ROOT, headers=AUTH)
            assert response.status_code == 401
            assert response.json()["code"] == "INVALID_TOKEN"
            assert response.json()["request_id"] == response.headers["x-request-id"]
            assert state["app"] is app_instance
        finally:
            identity_auth._firebase_app.cache_clear()

    def test_patient_own_and_other_records(self):
        http = setup_client(self)
        permissions = setup_permissions(self)
        setup_writes(self)
        response = http.post(ROOT, headers=AUTH, json=BP)
        assert response.status_code == 201
        assert response.json()["source"] == "patient"
        assert response.json()["secondary_value"] == 80
        assert (
            http.post(ROOT.replace(str(PATIENT), str(ACTOR)), headers=AUTH, json=BP).status_code
            == 404
        )
        permissions["account"] = "suspended"
        assert http.get(ROOT, headers=AUTH).status_code == 403

    def test_doctor_grant_and_active_consultation(self):
        http = setup_client(self)
        permissions = setup_permissions(self)
        setup_writes(self)

        async def list_active(*args):
            return {"items": [], "page": 1, "page_size": 20, "total": 0}

        self.enterContext(patch.object(router, "list_active", list_active))
        permissions.update(doctor=True, consultation=False)
        body = BP | {"consultation_id": str(CONSULTATION)}
        assert http.get(ROOT, headers=AUTH).status_code == 200
        assert http.post(ROOT, headers=AUTH, json=BP).status_code == 422
        assert http.post(ROOT, headers=AUTH, json=body).status_code == 403
        permissions["consultation"] = True
        response = http.post(ROOT, headers=AUTH, json=body)
        assert response.status_code == 201
        assert response.json()["source"] == "doctor"
        permissions["grant"] = False
        assert http.get(ROOT, headers=AUTH).status_code == 404

    def test_correction_replay_conflict_and_stale_version(self):
        http = setup_client(self)
        setup_permissions(self)
        writes = setup_writes(self)
        events, original = writes
        assert http.post(CORRECTION, headers=AUTH, json=FIX).status_code == 422
        response = http.post(CORRECTION, headers=KEY, json=FIX)
        assert response.status_code == 201
        assert response.json()["supersedes_id"] == str(READING)
        assert original["status"] == "superseded"
        assert http.post(CORRECTION, headers=KEY, json=FIX).json() == response.json()
        conflict = http.post(CORRECTION, headers=KEY, json=FIX | {"value": 119})
        assert (conflict.status_code, conflict.json()["code"]) == (409, "IDEMPOTENCY_CONFLICT")
        stale = http.post(
            CORRECTION, headers=KEY | {"Idempotency-Key": "review-test-002"}, json=FIX
        )
        assert (stale.status_code, stale.json()["code"]) == (409, "STALE_VERSION")
        assert events == [
            ("measurement.corrected", {"supersedes_id": str(READING), "source": "patient"})
        ]

    def test_professional_reading_cannot_be_corrected_by_patient(self):
        http = setup_client(self)
        setup_permissions(self)
        writes = setup_writes(self)
        writes[1]["source"] = "doctor"
        assert http.post(CORRECTION, headers=KEY, json=FIX).status_code == 403

    def test_list_filters_and_pagination(self):
        http = setup_client(self)
        setup_permissions(self)

        async def list_active(db, patient, kind, start, end, page, page_size):
            assert (patient, kind, page, page_size) == (PATIENT, "blood_pressure", 2, 10)
            assert start == datetime.fromisoformat(TIME)
            assert end > start
            return {"items": [], "page": page, "page_size": page_size, "total": 0}

        self.enterContext(patch.object(router, "list_active", list_active))
        response = http.get(
            ROOT,
            headers=AUTH,
            params={
                "type": "blood_pressure",
                "from": TIME,
                "to": "2025-01-02T00:00:00Z",
                "page": 2,
                "page_size": 10,
            },
        )
        assert response.status_code == 200
        assert response.json()["page_size"] == 10
        for params in ({"from": TIME}, {"page_size": 101}, {"unknown": "1"}):
            assert http.get(ROOT, headers=AUTH, params=params).status_code == 422
