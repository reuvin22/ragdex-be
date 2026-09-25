"""Historical candles for the chart on a trade record."""

from __future__ import annotations

from fastapi import APIRouter, Query

from app.controllers.deps import AppSettings, MarketRateLimit, ReadUser
from app.core.errors import AppError
from app.models.schemas.common import ErrorResponse
from app.models.schemas.market import MAX_WINDOW_DAYS, Candle, CandlesResponse
from app.services import market

router = APIRouter(
    prefix="/market",
    tags=["market"],
    dependencies=[MarketRateLimit],
    responses={401: {"model": ErrorResponse}, 429: {"model": ErrorResponse}},
)

#: Longest window in milliseconds, from the schema's day cap.
_MAX_WINDOW_MS = MAX_WINDOW_DAYS * 24 * 60 * 60 * 1000

#: A ticker is free text in the journal, so it is bounded here before it is
#: ever put in a URL. Length only — the provider decides what is real.
_MAX_TICKER = 16


@router.get(
    "/candles",
    response_model=CandlesResponse,
    summary="Candles covering one trade's window",
)
async def candles(
    user: ReadUser,
    settings: AppSettings,
    ticker: str = Query(min_length=1, max_length=_MAX_TICKER),
    from_ms: int = Query(alias="from", ge=0, description="Window start, epoch ms."),
    to_ms: int = Query(alias="to", ge=0, description="Window end, epoch ms."),
) -> CandlesResponse:
    """Bars for a symbol over a window, at a span chosen from the window's size.

    A session is required, but no part of the answer depends on who is asking:
    candles are public market facts and the cache behind this is shared. The
    session is here to keep the endpoint — and the provider quota behind it —
    off the open internet, not to scope the data.

    The caller does not choose the bar size. A trade held ninety seconds and
    one held three weeks both come through here, and letting a client ask for
    one-minute bars across a year is how a single request becomes a very large
    upstream bill.
    """
    # `user` is intentionally not read. It stays in the signature because this
    # codebase shows what a route requires there — "reads use ReadUser" — and
    # a session enforced only as a side effect of the rate-limit dependency
    # would be a session that vanishes silently the day that dependency moves.
    if to_ms <= from_ms:
        raise AppError(
            "The end of the window must come after its start.",
            status_code=422,
            code="invalid_window",
        )

    if to_ms - from_ms > _MAX_WINDOW_MS:
        raise AppError(
            f"That window is longer than {MAX_WINDOW_DAYS} days.",
            status_code=422,
            code="window_too_long",
        )

    bars, span = await market.candles(
        ticker=ticker, from_ms=from_ms, to_ms=to_ms, settings=settings
    )

    return CandlesResponse(
        ticker=ticker.strip().upper(),
        span=span,  # type: ignore[arg-type]
        candles=[
            Candle(
                time=bar.time,
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                volume=bar.volume,
            )
            for bar in bars
        ],
        empty=not bars,
    )
