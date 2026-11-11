
"""Tests for ML-36 document error responses."""

import json
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from app.modules.documents.errors import (
    DocumentError,
    document_error_handler,
)
from app.modules.documents.router import download_document
from app.modules.documents.service import _reject


class TestDocumentErrorContract(IsolatedAsyncioTestCase):

    async def test_not_found_response(self):
        request = MagicMock()
        request.state.request_id = "test-123"

        with self.assertRaises(DocumentError) as context:
            _reject()

        response = await document_error_handler(
            request, context.exception
        )

        body = json.loads(response.body)

        self.assertEqual(response.status_code, 404)
        self.assertEqual(
            body["code"], "RESOURCE_NOT_FOUND"
        )
        self.assertEqual(body["request_id"], "test-123")

    async def test_invalid_state_response(self):
        request = MagicMock()
        request.state.request_id = "test-456"

        with self.assertRaises(DocumentError) as context:
            _reject(
                409,
                "INVALID_STATE",
                "Document cannot be replaced.",
            )

        response = await document_error_handler(
            request, context.exception
        )

        body = json.loads(response.body)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(body["code"], "INVALID_STATE")

    async def test_download_missing_content_returns_resource_not_found(self):
        request = MagicMock()
        request.state.request_id = "test-789"

        db = MagicMock()
        actor = MagicMock()
        doc = MagicMock()

        doc.storage_key = "0123456789abcdef0123456789abcdef"

        with (
            patch(
                "app.modules.documents.router.service.permitted_document",
                new_callable=AsyncMock,
                return_value=doc,
            ),
            patch(
                "app.modules.documents.router.run_in_threadpool",
                new_callable=AsyncMock,
                side_effect=FileNotFoundError,
            ),
        ):
            with self.assertRaises(DocumentError) as context:
                await download_document(
                    uuid4(),
                    request,
                    db,
                    actor,
                )

        response = await document_error_handler(
            request,
            context.exception,
        )

        body = json.loads(response.body)

        self.assertEqual(response.status_code, 404)
        self.assertEqual(
            body["code"],
            "RESOURCE_NOT_FOUND",
        )
        self.assertEqual(
            body["message"],
            "Document content is unavailable.",
        )
        self.assertEqual(
            body["request_id"],
            "test-789",
        )
