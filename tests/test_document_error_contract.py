
import json
from unittest import IsolatedAsyncioTestCase
from unittest.mock import MagicMock

from app.modules.documents.errors import (
    DocumentError,
    document_error_handler,
)
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
