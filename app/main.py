"""Application factory.

Nothing but wiring: settings, middleware order, routers, lifecycle. Anything
with a decision in it belongs in ``core``, ``services`` or ``repositories``,
which is what keeps this file readable as a description of the whole app.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.api.v1.router import api_router
from app.api.v1.routes import health
from app.core.config import Settings, get_settings
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging
from app.core.middleware import (
    BodySizeLimitMiddleware,
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
)
from app.db.firestore import init_firebase

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = get_settings()

    # Fail at boot rather than on the first request: a bad service account
    # should stop a deploy, not surface as a 500 to whoever arrives first.
    init_firebase()
    logger.info("Started in %s", settings.environment)

    yield

    logger.info("Shutting down")


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(json_logs=settings.is_production)

    app = FastAPI(
        title=settings.project_name,
        version=health.VERSION,
        lifespan=lifespan,
        # Interactive docs are a map of the API. Useful in development,
        # needless attack surface in production.
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None,
        openapi_url=None if settings.is_production else "/openapi.json",
    )

    # Order matters, and it reads bottom-up: the last added is the outermost.
    # Request context must wrap everything so even a rejected request is
    # logged with an id.
    app.add_middleware(GZipMiddleware, minimum_size=1_000)
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_request_bytes)
    app.add_middleware(SecurityHeadersMiddleware, hsts=settings.is_production)

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=True,
            # Only what the client actually sends. A wildcard here would let
            # any header through on a credentialed request.
            allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
            expose_headers=["X-Request-ID"],
            max_age=600,
        )

    if settings.allowed_hosts != ["*"]:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)

    app.add_middleware(RequestContextMiddleware)

    register_exception_handlers(app)

    app.include_router(health.router)
    app.include_router(api_router, prefix=settings.api_v1_prefix)

    return app


app = create_app()
