"""Version 1 of the API.

Routers are assembled here rather than in ``main`` so the app factory stays
about wiring and a new version is a new module, not an edit to the old one.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.controllers.v1 import (
    auth,
    brokers,
    chat,
    coach,
    community,
    imports,
    insights,
    market,
    profile,
    trades,
    university,
    uploads,
)

api_router = APIRouter()
# Auth first: it is the only family reachable without a session.
api_router.include_router(auth.router)
api_router.include_router(profile.router)
api_router.include_router(trades.router)
api_router.include_router(coach.router)
api_router.include_router(insights.router)
api_router.include_router(market.router)
api_router.include_router(chat.router)
api_router.include_router(uploads.router)
api_router.include_router(imports.router)
api_router.include_router(brokers.router)
api_router.include_router(university.router)
api_router.include_router(community.router)
