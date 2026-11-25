"""Keep the checked-in grant contract aligned with the live response schema."""

import json
from pathlib import Path

from app.main import app


def test_doctor_display_name_is_optional_and_nullable_in_both_contracts():
    contract_path = Path(__file__).resolve().parents[1] / "docs/reference-contract/03_OPENAPI.json"
    checked_in = json.loads(contract_path.read_text())["components"]["schemas"]["GrantDetail"]
    live = app.openapi()["components"]["schemas"]["GrantDetail"]

    documented_field = checked_in["properties"]["doctor_display_name"]
    assert documented_field == {"type": "string", "nullable": True}
    assert "doctor_display_name" not in checked_in.get("required", [])
    assert checked_in["additionalProperties"] is False

    live_field = live["properties"]["doctor_display_name"]
    assert {entry["type"] for entry in live_field["anyOf"]} == {"string", "null"}
    assert "doctor_display_name" not in live.get("required", [])
