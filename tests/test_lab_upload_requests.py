from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi import HTTPException

from app.modules.laboratory import service


class LabUploadRequestResolveTests(IsolatedAsyncioTestCase):
    async def test_unresolvable_token_returns_404(self):
        db = AsyncMock()
        actor = SimpleNamespace(id=uuid4())
        laboratory_id = uuid4()
        token = "test-upload-token"

        with (
            patch.object(service, "require_verified_membership", new_callable=AsyncMock),
            patch.object(
                service.repository,
                "find_resolvable_request",
                new_callable=AsyncMock,
                return_value=None,
            ) as lookup,
        ):
            with self.assertRaises(service.LabUploadRequestError) as error:
                await service.resolve_request(db, actor, laboratory_id, token, "test-request")

        self.assertEqual(error.exception.status_code, 404)
        self.assertEqual(lookup.await_args.kwargs["laboratory_id"], laboratory_id)
        self.assertEqual(lookup.await_args.kwargs["token_hash"], service.hash_token(token))
        db.commit.assert_not_awaited()

    async def test_staff_without_matching_membership_cannot_look_up_token(self):
        db = AsyncMock()
        actor = SimpleNamespace(id=uuid4())
        laboratory_id = uuid4()

        with (
            patch.object(
                service,
                "require_verified_membership",
                new_callable=AsyncMock,
                side_effect=HTTPException(status_code=403, detail="Access denied"),
            ) as membership,
            patch.object(
                service.repository, "find_resolvable_request", new_callable=AsyncMock
            ) as lookup,
        ):
            with self.assertRaises(HTTPException) as error:
                await service.resolve_request(
                    db, actor, laboratory_id, "test-upload-token", "test-request"
                )

        self.assertEqual(error.exception.status_code, 403)
        membership.assert_awaited_once_with(db, actor, "lab_staff", laboratory_id)
        lookup.assert_not_awaited()