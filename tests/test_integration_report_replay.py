"""Regression test for auditing a repeated integration report submission."""
from io import BytesIO
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.database.session import get_session
from app.modules.integrations import report_service
from app.modules.integrations.auth import get_active_integration_client
from app.modules.integrations.report_router import router


class IntegrationReportReplayTests(IsolatedAsyncioTestCase):
    async def test_same_key_replays_original_response_and_audits_retry(self):
        client_id = uuid4()
        submission_id = uuid4()
        original = {
            'id': str(submission_id),
            'client_id': str(client_id),
            'upload_request_id': str(uuid4()),
            'external_report_id': 'LAB-001',
            'idempotency_key': 'retry-key-001',
            'status': 'accepted',
            'report_id': str(uuid4()),
            'received_at': '2026-11-25T00:00:00Z',
            'error_code': None,
        }
        db = AsyncMock()
        db.get.return_value = SimpleNamespace(status='active', type='laboratory')
        client = SimpleNamespace(id=client_id, organization_id=uuid4())
        app = FastAPI()
        app.include_router(router, prefix='/api/v1')
        app.dependency_overrides[get_session] = lambda: db
        app.dependency_overrides[get_active_integration_client] = lambda: client

        @app.middleware('http')
        async def request_id(request, call_next):
            request.state.request_id = 'retry-test-request'
            return await call_next(request)

        fields = {'request_token': 'test-token', 'external_report_id': 'LAB-001',
                  'report_type': 'FBC', 'lab_reference': 'LAB-REF-1'}
        with (
            patch.object(report_service, 'lock_and_replay', new_callable=AsyncMock,
                         return_value={'response_body': original}),
            patch.object(report_service, 'append_event', new_callable=AsyncMock) as audit,
            patch.object(report_service.storage, 'save', new_callable=AsyncMock) as save,
            patch('app.modules.integrations.report_router.validate_document_upload',
                  new_callable=AsyncMock, return_value='application/pdf'),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as http:
                response = await http.post('/api/v1/integrations/reports', data=fields,
                    files={'file': ('report.pdf', BytesIO(b'synthetic file'), 'application/pdf')},
                    headers={'Idempotency-Key': 'retry-key-001'})

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json(), original)
        audit.assert_awaited_once_with(
            db, actor_client_id=client_id, action='integration.report_replayed',
            target_type='integration_submission', target_id=submission_id,
            outcome='success', request_id='retry-test-request')
        db.commit.assert_awaited_once()
        save.assert_not_awaited()
