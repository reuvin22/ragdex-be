"""Liveness and readiness.

Unauthenticated on purpose — a load balancer has no token — which is why they
report nothing about the system beyond whether it can serve.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Response, status

from app.controllers.deps import AppSettings
from app.core.config import get_settings
from app.db.firestore import get_client
from app.models.schemas.common import HealthResponse, ReadyResponse
from app.services import email as email_service

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])

VERSION = "1.0.0"


@router.get("/health", response_model=HealthResponse, summary="Liveness")
async def health(settings: AppSettings) -> HealthResponse:
    """The process is up. No dependencies are touched."""
    return HealthResponse(environment=settings.environment, version=VERSION)


@router.get("/ready", response_model=ReadyResponse, summary="Readiness")
async def ready(response: Response) -> ReadyResponse:
    """Whether the things a request needs are actually reachable."""
    checks: dict[str, str] = {}
    firestore_ok = True

    try:
        # Cheapest call that proves credentials and connectivity.
        next(get_client().collections(), None)
        checks["firestore"] = "ok"
    except Exception as exc:
        firestore_ok = False
        checks["firestore"] = type(exc).__name__
        logger.warning("Readiness check failed: %s", type(exc).__name__)

    # Reported, not enforced. A missing mail key breaks sign-up confirmation and
    # nothing else, so it must not take the service out of rotation — but it is
    # the kind of thing that is otherwise only discovered by a user failing to
    # get in, so it belongs somewhere you can curl.
    settings = get_settings()

    absent = email_service.missing_settings(settings)
    checks["email"] = "ok" if not absent else "missing " + ", ".join(absent)

    # Same treatment: no key means the coach and the leak card answer 501 while
    # everything else works, which is a deployment gap rather than an outage —
    # but one that otherwise only shows up when somebody asks the coach a
    # question and gets nothing back.
    checks["coach"] = (
        "ok" if settings.openrouter_api_key is not None else "missing OPENROUTER_API_KEY"
    )

    # And again for image storage: without it, uploading a chart or a chat
    # photo answers 501 and the rest of the app is unaffected. Named field by
    # field, because "R2 is not configured" when three of the four are set is
    # the least useful thing this endpoint could say.
    r2_missing = [
        name
        for name, value in (
            ("R2_ACCOUNT_ID", settings.r2_account_id),
            ("R2_ACCESS_KEY_ID", settings.r2_access_key_id),
            ("R2_SECRET_ACCESS_KEY", settings.r2_secret_access_key),
            ("R2_BUCKET", settings.r2_bucket),
        )
        if not value
    ]
    checks["images"] = "ok" if not r2_missing else "missing " + ", ".join(r2_missing)

    if not firestore_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return ReadyResponse(
        status="ready" if firestore_ok else "degraded",
        firestore=firestore_ok,
        checks=checks,
    )
