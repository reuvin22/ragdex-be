"""Behavioural analysis over the trader's own journal."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import AppSettings, CoachRateLimit, ReadUser
from app.repositories import trades as trades_repo
from app.schemas.coach import LeakResponse
from app.schemas.common import ErrorResponse
from app.services.insights import detect_leak
from app.services.stats import MIN_TRADES_FOR_ANALYSIS, summarise

router = APIRouter(
    prefix="/insights",
    tags=["insights"],
    dependencies=[CoachRateLimit],
    responses={401: {"model": ErrorResponse}, 429: {"model": ErrorResponse}},
)


@router.get(
    "/behavioral-leak",
    response_model=LeakResponse,
    summary="The most expensive habit in the journal",
)
async def behavioral_leak(user: ReadUser, settings: AppSettings) -> LeakResponse:
    """Returns ``needed`` instead of a result when the journal is too short to
    say anything honest — the client shows that as "log a few more"."""
    trades = trades_repo.all_trades(user.uid)

    if len(trades) < MIN_TRADES_FOR_ANALYSIS:
        return LeakResponse(
            result=None, trade_count=len(trades), needed=MIN_TRADES_FOR_ANALYSIS
        )

    result = await detect_leak(summarise(trades), settings)
    return LeakResponse(result=result, trade_count=len(trades))
