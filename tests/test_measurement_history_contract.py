
"""ML-102 measurement history API contract tests."""

from app.main import app


def test_history_endpoint_registered():
    paths = app.openapi()["paths"]

    assert "/api/v1/patients/{patient_id}/measurements/history" in paths
    assert "get" in paths["/api/v1/patients/{patient_id}/measurements/history"]


def test_measurement_correction_endpoint_unchanged():
    paths = app.openapi()["paths"]

    assert "/api/v1/measurements/{measurement_id}/corrections" in paths
    assert "post" in paths["/api/v1/measurements/{measurement_id}/corrections"]
