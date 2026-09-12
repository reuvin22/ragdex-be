"""One error shape for the whole API.

Clients get a stable envelope and a correlation id; they never get a traceback,
a database path, or the text of an upstream provider's error. Anything worth
diagnosing goes to the log, where the same request id makes it findable.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from google.api_core.exceptions import FailedPrecondition
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)


class AppError(Exception):
    """A failure we chose to surface, with wording fit for a person."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int = status.HTTP_400_BAD_REQUEST,
        code: str = "bad_request",
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


class NotFoundError(AppError):
    def __init__(self, message: str = "Not found.") -> None:
        super().__init__(message, status_code=status.HTTP_404_NOT_FOUND, code="not_found")


class PermissionDeniedError(AppError):
    def __init__(self, message: str = "You cannot do that.") -> None:
        super().__init__(
            message, status_code=status.HTTP_403_FORBIDDEN, code="forbidden"
        )


class UpstreamError(AppError):
    """A dependency failed. The detail is logged, not returned."""

    def __init__(self, message: str = "An upstream service failed.") -> None:
        super().__init__(
            message, status_code=status.HTTP_502_BAD_GATEWAY, code="upstream_error"
        )


def _payload(code: str, message: str, request: Request, **extra: Any) -> dict[str, Any]:
    return {
        "error": {
            "code": code,
            "message": message,
            "request_id": getattr(request.state, "request_id", None),
            **extra,
        }
    }


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=_payload(exc.code, exc.message, request),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        detail = exc.detail if isinstance(exc.detail, str) else "Request failed."
        return JSONResponse(
            status_code=exc.status_code,
            content=_payload("http_error", detail, request),
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # Field errors are safe and genuinely useful to a client; the raw input
        # is not echoed back, because it may contain whatever was posted.
        fields = [
            {"field": ".".join(str(part) for part in err["loc"][1:]), "issue": err["msg"]}
            for err in exc.errors()
        ]
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content=jsonable_encoder(
                _payload(
                    "validation_error",
                    "Some fields were not accepted.",
                    request,
                    fields=fields,
                )
            ),
        )

    @app.exception_handler(FailedPrecondition)
    async def _failed_precondition(
        request: Request, exc: FailedPrecondition
    ) -> JSONResponse:
        # Firestore raises this when a query needs a composite index that has
        # not been deployed yet, or is still building. That is a deployment gap
        # rather than a bad request, and as a bare 500 it cost a traceback to
        # identify something the error already stated plainly. The message
        # Firestore returns carries a console URL that creates the index, so it
        # goes to the log verbatim — and no further, since it names collections
        # and fields.
        detail = getattr(exc, "message", str(exc))
        missing_index = "index" in detail.lower()

        logger.error(
            "Firestore refused a query: %s",
            detail,
            extra={"request_id": getattr(request.state, "request_id", None)},
        )

        message = (
            "The database is still preparing this query. Try again in a few minutes."
            if missing_index
            else "The database refused that request."
        )
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content=_payload(
                "database_not_ready" if missing_index else "database_error",
                message,
                request,
            ),
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        # exc_info goes to the log, never to the response.
        logger.exception(
            "Unhandled error",
            extra={"request_id": getattr(request.state, "request_id", None)},
            exc_info=exc,
        )
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=_payload(
                "internal_error", "Something went wrong on our side.", request
            ),
        )
