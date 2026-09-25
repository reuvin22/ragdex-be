"""Behavioural analysis over the trader's own journal."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Query

from app.controllers.deps import AppSettings, CoachRateLimit, ReadUser
from app.models.repositories import insights as insights_repo
from app.models.repositories import profiles as profiles_repo
from app.models.repositories import trades as trades_repo
from app.models.schemas.coach import LeakResponse
from app.models.schemas.common import ErrorResponse
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
async def behavioral_leak(
    user: ReadUser,
    settings: AppSettings,
    refresh: bool = Query(
        default=False,
        description="Re-analyse now, whatever the cadence says. Behind the "
        "card's refresh button, which is a deliberate act rather than a "
        "page load.",
    ),
) -> LeakResponse:
    """Returns ``needed`` instead of a result when the journal is too short to
    say anything honest — the client shows that as "log a few more".

    Otherwise the answer usually comes from the store. The analysis is
    recomputed only when the trader's chosen cadence has come round, or when
    they ask for it — this route used to spend a model call on every dashboard
    load, which is both an expense and, on a free key, the fastest route to
    being rate limited out of the feature.
    """
    trades = trades_repo.all_trades(user.uid)

    if len(trades) < MIN_TRADES_FOR_ANALYSIS:
        return LeakResponse(
            result=None, trade_count=len(trades), needed=MIN_TRADES_FOR_ANALYSIS
        )

    profile = profiles_repo.get_profile(user.uid)
    stored = insights_repo.load(user.uid)

    if stored is not None and not refresh:
        # Named apart from the freshly computed one below. Reusing one name for
        # a stored LeakResult and an Optional from the model is what let mypy
        # see the None check further down as unreachable.
        cached, cached_at, _ = stored
        due = insights_repo.next_due(cached_at, profile.leak_cadence, profile.timezone)
        if datetime.now(UTC) < due:
            return LeakResponse(
                result=cached,
                trade_count=len(trades),
                computed_at=cached_at,
                next_at=due,
                cadence=profile.leak_cadence,
            )

    fresh = await detect_leak(summarise(trades), settings)

    if fresh is None:
        return LeakResponse(result=None, trade_count=len(trades))

    computed_at = insights_repo.store(user.uid, fresh, trade_count=len(trades))
    return LeakResponse(
        result=fresh,
        trade_count=len(trades),
        computed_at=computed_at,
        next_at=insights_repo.next_due(
            computed_at, profile.leak_cadence, profile.timezone
        ),
        cadence=profile.leak_cadence,
    )
