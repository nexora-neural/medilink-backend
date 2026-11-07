"""Contract errors and privacy headers for profile endpoints."""

from fastapi import HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from app.core.rate_limits import RateLimitExceeded, rate_limit_exceeded_handler


class ProfileRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def route(request: Request):
            request_id = getattr(request.state, "request_id", "")
            try:
                response = await handler(request)
            except RequestValidationError as exc:
                validation_handler = request.app.exception_handlers[
                    RequestValidationError
                ]
                response = await validation_handler(request, exc)
            except RateLimitExceeded as exc:
                response = await rate_limit_exceeded_handler(request, exc)
            except HTTPException as exc:
                if isinstance(exc.detail, dict):
                    content = exc.detail
                else:
                    codes = {
                        400: "VALIDATION_ERROR",
                        401: "AUTHENTICATION_REQUIRED",
                        403: "ACCESS_DENIED",
                        404: "RESOURCE_NOT_FOUND",
                        409: "INVALID_STATE",
                        503: "SERVICE_UNAVAILABLE",
                    }
                    content = {
                        "code": codes.get(exc.status_code, "REQUEST_FAILED"),
                        "message": str(exc.detail),
                        "field_errors": {},
                        "request_id": request_id,
                    }
                response = JSONResponse(
                    status_code=exc.status_code,
                    content=content,
                    headers=exc.headers,
                )
            except Exception:
                response = JSONResponse(
                    status_code=500,
                    content={
                        "code": "INTERNAL_ERROR",
                        "message": "An internal error occurred.",
                        "field_errors": {},
                        "request_id": request_id,
                    },
                )

            response.headers["Cache-Control"] = "no-store"
            return response

        return route