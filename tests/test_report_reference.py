from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.modules.laboratory.report_service import read_report


@pytest.mark.asyncio
@pytest.mark.parametrize("laboratory", [True, False])
async def test_reference_is_read_from_persisted_identity(laboratory):
    identity_id = uuid4() if laboratory else None
    report = SimpleNamespace(
        id=uuid4(), patient_user_id=uuid4(), laboratory_id=uuid4() if laboratory else None,
        report_identity_id=identity_id, upload_request_id=None, document_id=uuid4(),
        report_type="FBC", report_date=None, notes="Saved notes",
        source="lab_staff" if laboratory else "patient", uploaded_by=uuid4(),
        status="active", created_at=datetime.now(UTC), supersedes_id=None,
        correction_reason=None, withdrawal_reason=None,
    )
    db = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(lab_reference="LAB-42")))
    result = await read_report(db, report)
    assert result.lab_reference == ("LAB-42" if laboratory else None)
    assert result.notes == "Saved notes"
    if laboratory:
        db.get.assert_awaited_once()
    else:
        db.get.assert_not_awaited()