"""Cross-cutting request handling.

Deliberately small: each of these is a rule that has to hold for every route,
which is exactly the set of things worth putting in middleware rather than in
a dependency.
"""

from __future__ import annotations

import logging
import time
import uuid

from starlette.datastructures import Headers
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.middleware.gzip import GZipMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.status import HTTP_413_REQUEST_ENTITY_TOO_LARGE
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.errors import AppError
from app.core.logging import request_id_var

logger = logging.getLogger(__name__)


class PayloadTooLarge(AppError):
    """Raised while the body is being read, once it passes the ceiling."""

    def __init__(self) -> None:
        super().__init__(
            "That request was too large.",
            status_code=HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            code="payload_too_large",
        )


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Give every request an id, log its outcome, and hand the id back."""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        # Trust an inbound id only as far as shape: a proxy may set it, but it
        # ends up in logs, so it must not be arbitrary attacker-controlled text.
        inbound = request.headers.get("x-request-id", "")
        request_id = (
            inbound if inbound.isalnum() and len(inbound) <= 64 else uuid.uuid4().hex
        )

        request.state.request_id = request_id
        token = request_id_var.set(request_id)
        started = time.perf_counter()

        try:
            response = await call_next(request)
        finally:
            request_id_var.reset(token)

        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        response.headers["X-Request-ID"] = request_id

        logger.info(
            "%s %s -> %s",
            request.method,
            request.url.path,
            response.status_code,
            extra={
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "duration_ms": duration_ms,
            },
        )
        return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Headers that matter even for a JSON API.

    No CSP: this serves no HTML. The rest close off content sniffing, framing,
    and referrer leakage of API paths.
    """

    def __init__(self, app: ASGIApp, *, hsts: bool) -> None:
        super().__init__(app)
        self.hsts = hsts

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        response = await call_next(request)
        headers = response.headers

        headers.setdefault("X-Content-Type-Options", "nosniff")
        headers.setdefault("X-Frame-Options", "DENY")
        headers.setdefault("Referrer-Policy", "no-referrer")
        headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        headers.setdefault("Cross-Origin-Resource-Policy", "same-site")
        headers.setdefault(
            "Permissions-Policy", "geolocation=(), microphone=(), camera=()"
        )
        # Responses are per-user; a shared cache must never reuse one.
        headers.setdefault("Cache-Control", "no-store")

        if self.hsts:
            headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )

        return response


class BodySizeLimitMiddleware:
    """Refuse oversized bodies before they are parsed.

    Written as raw ASGI rather than on ``BaseHTTPMiddleware`` on purpose. The
    only way to bound a request that declares no length is to count the body
    as it streams, which means wrapping ``receive`` — and a BaseHTTPMiddleware
    cannot hand a replacement ``receive`` to the app underneath it. Buffering
    the stream there and rebuilding the Request does not work either: the
    route reads from the original channel and finds it empty.

    Content-Length is checked first because it is free and covers every
    ordinary client. The streaming count is the backstop for chunked uploads,
    which are the ones that could otherwise arrive unbounded.
    """

    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        declared = Headers(scope=scope).get("content-length")
        if declared is not None:
            try:
                too_big = int(declared) > self.max_bytes
            except ValueError:
                too_big = True
            if too_big:
                await self._refuse(scope, receive, send)
                return

        received = 0

        async def counted_receive() -> Message:
            nonlocal received

            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    # Raised inside the route's own body read, so the AppError
                    # handler turns it into the usual 413 envelope.
                    raise PayloadTooLarge()
            return message

        await self.app(scope, counted_receive, send)

    async def _refuse(self, scope: Scope, receive: Receive, send: Send) -> None:
        response = JSONResponse(
            status_code=HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            content={
                "error": {
                    "code": "payload_too_large",
                    "message": "That request was too large.",
                    "request_id": scope.get("state", {}).get("request_id"),
                }
            },
        )
        await response(scope, receive, send)


class SelectiveGZipMiddleware:
    """GZip, except on paths that stream.

    ``GZipMiddleware`` wraps a streaming response and compresses it in blocks,
    which is right for a large JSON body and wrong for server-sent events: the
    events sit in the compressor's buffer instead of reaching the browser, and
    a live stream silently behaves like a slow one. Excluding the stream path
    is simpler and more predictable than trying to make gzip flush per event —
    an SSE line is a few dozen bytes, so there is nothing worth compressing.
    """

    def __init__(
        self, app: ASGIApp, *, minimum_size: int, exclude_prefixes: tuple[str, ...]
    ) -> None:
        self.app = app
        self.gzip = GZipMiddleware(app, minimum_size=minimum_size)
        self.exclude_prefixes = exclude_prefixes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and str(scope.get("path", "")).startswith(
            self.exclude_prefixes
        ):
            await self.app(scope, receive, send)
            return

        await self.gzip(scope, receive, send)
