"""Liveness and readiness.

Unauthenticated on purpose — a load balancer has no token — which is why they
report nothing about the system beyond whether it can serve.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Response, status

from app.api.deps import AppSettings
from app.db.firestore import get_client
from app.schemas.common import HealthResponse, ReadyResponse

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
    except Exception as exc:  # noqa: BLE001 - readiness must never raise
        firestore_ok = False
        checks["firestore"] = type(exc).__name__
        logger.warning("Readiness check failed: %s", type(exc).__name__)

    if not firestore_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return ReadyResponse(
        status="ready" if firestore_ok else "degraded",
        firestore=firestore_ok,
        checks=checks,
    )
