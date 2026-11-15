"""ML-102 history route contract checks. Uses FastAPI route registration; no live DB."""
from app.main import app


def test_history_endpoint_registered():
    paths = {
        (route.path, method)
        for route in app.routes
        for method in getattr(route, "methods", set())
    }
    assert ("/api/v1/patients/{patient_id}/measurements/history", "GET") in paths


def test_measurement_correction_endpoint_unchanged():
    paths = {
        (route.path, method)
        for route in app.routes
        for method in getattr(route, "methods", set())
    }
    assert ("/api/v1/measurements/{measurement_id}/corrections", "POST") in paths
