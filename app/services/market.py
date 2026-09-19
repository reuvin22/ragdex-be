"""Historical candles for the chart on a trade record.

The client cannot fetch these itself. A market data provider needs a key, and
a key in the browser is a key anyone can read and spend — so the request comes
here, this service adds the credential, and the browser keeps its single
outbound destination.

Massive — the company that was Polygon.io until it rebranded in October 2025 —
is the default, because it is the one provider with a usable free tier for
what this is actually for: *intraday* history, years back. A trade held an
hour needs minute bars from the day it happened, and most free tiers serve
either recent intraday or long-range daily, neither of which can draw that
trade. Swapping providers means rewriting `_url` and `_parse` and nothing
else; the route and the schema do not know who answered.

Their free tier reaches back two years and their cheapest paid tier five, so
a journal older than that will legitimately come back empty for its earliest
trades. That is not a failure and the client does not present it as one.

Nothing here is per-user. Candles are public market facts, identical for every
trader, which is why the cache below is global and why this service never
takes a uid.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

from app.core.config import Settings
from app.core.errors import AppError, UpstreamError

logger = logging.getLogger(__name__)

# The post-rebrand host. `api.polygon.io` still answers and still accepts the
# same keys, but the provider has said it is being retired, so new code should
# not be written against it. The path and response shape are unchanged.
_BASE = "https://api.massive.com"

#: Bar sizes this service will serve, mapped to Polygon's multiplier/timespan.
#: Deliberately a closed set: the span is chosen here from the length of the
#: trade, never passed in, so a caller cannot ask for a million one-second bars.
_SPANS: dict[str, tuple[int, str]] = {
    "1m": (1, "minute"),
    "5m": (5, "minute"),
    "15m": (15, "minute"),
    "1h": (1, "hour"),
    "1d": (1, "day"),
}

#: The most bars one response may carry. A chart 900px wide cannot show more,
#: and the cap is what stops a long date range becoming a slow, enormous body.
MAX_BARS = 1_000


@dataclass(slots=True, frozen=True)
class Candle:
    """One bar. Times are epoch milliseconds, as the provider gives them."""

    time: int
    open: float
    high: float
    low: float
    close: float
    volume: float


def span_for(from_ms: int, to_ms: int) -> str:
    """The bar size that makes a window legible.

    A trade held ninety seconds and a trade held three weeks are both "one
    trade" to the journal, and a fixed bar size serves one of them terribly.
    The window picks the span so the chart lands in the low hundreds of bars
    either way.
    """
    minutes = max((to_ms - from_ms) / 60_000, 1)

    if minutes <= 180:
        return "1m"
    if minutes <= 900:
        return "5m"
    if minutes <= 3_000:
        return "15m"
    if minutes <= 43_200:  # thirty days
        return "1h"
    return "1d"


# ---------------------------------------------------------------- cache

#: A candle that has closed never changes, so this can be held for a long time
#: and shared by every trader looking at the same symbol and window. It is the
#: difference between one provider call per journal and one per modal open,
#: which on a five-calls-a-minute free tier is the difference between working
#: and not.
#:
#: In-process, like the rate limiter, and with the same caveat: it is a guard
#: rail for a single instance, not a shared cache. More than one replica means
#: moving it to Redis.
_CACHE: dict[tuple[str, str, int, int], tuple[float, list[Candle]]] = {}
_CACHE_TTL_SECONDS = 60 * 60 * 12
#: Bounded so a long-running instance cannot grow it without limit.
_CACHE_MAX_ENTRIES = 512


def _cache_get(key: tuple[str, str, int, int]) -> list[Candle] | None:
    entry = _CACHE.get(key)
    if entry is None:
        return None

    stored_at, candles = entry
    if time.monotonic() - stored_at > _CACHE_TTL_SECONDS:
        _CACHE.pop(key, None)
        return None

    return candles


def _cache_put(key: tuple[str, str, int, int], candles: list[Candle]) -> None:
    if len(_CACHE) >= _CACHE_MAX_ENTRIES:
        # Oldest insertion first: dicts keep insertion order, and a rough
        # eviction is enough for something this cheap to recompute.
        _CACHE.pop(next(iter(_CACHE)), None)
    _CACHE[key] = (time.monotonic(), candles)


# ------------------------------------------------------------- provider


def _url(*, ticker: str, span: str, from_ms: int, to_ms: int) -> str:
    multiplier, timespan = _SPANS[span]
    return (
        f"{_BASE}/v2/aggs/ticker/{ticker}/range/{multiplier}/{timespan}"
        f"/{from_ms}/{to_ms}"
    )


def _parse(payload: dict[str, Any]) -> list[Candle]:
    """Polygon's aggregate shape, which uses one-letter keys."""
    rows = payload.get("results") or []
    candles: list[Candle] = []

    for row in rows:
        try:
            candles.append(
                Candle(
                    time=int(row["t"]),
                    open=float(row["o"]),
                    high=float(row["h"]),
                    low=float(row["l"]),
                    close=float(row["c"]),
                    volume=float(row.get("v", 0.0)),
                )
            )
        except (KeyError, TypeError, ValueError):
            # One malformed bar should not lose the other four hundred.
            logger.warning("Skipped a malformed candle in the provider response")

    candles.sort(key=lambda candle: candle.time)
    return candles


async def candles(
    *, ticker: str, from_ms: int, to_ms: int, settings: Settings
) -> tuple[list[Candle], str]:
    """Bars covering the window, and the span they are in.

    Raises when the provider is unconfigured or unreachable. An empty list is
    not an error: a symbol the provider does not carry, or a window falling
    entirely outside market hours, legitimately has no bars, and the client
    shows that as "no market data for this trade" rather than as a failure.
    """
    if settings.market_api_key is None:
        raise AppError(
            "Market data is not configured for this deployment.",
            status_code=503,
            code="market_unconfigured",
        )

    symbol = ticker.strip().upper()
    if not symbol:
        return [], "1m"

    span = span_for(from_ms, to_ms)
    key = (symbol, span, from_ms, to_ms)

    cached = _cache_get(key)
    if cached is not None:
        return cached, span

    try:
        async with httpx.AsyncClient(timeout=settings.market_timeout_seconds) as client:
            response = await client.get(
                _url(ticker=symbol, span=span, from_ms=from_ms, to_ms=to_ms),
                params={
                    "adjusted": "true",
                    "sort": "asc",
                    "limit": MAX_BARS,
                    "apiKey": settings.market_api_key.get_secret_value(),
                },
            )
    except httpx.TimeoutException as exc:
        logger.warning(
            "Market provider timed out after %ss", settings.market_timeout_seconds
        )
        raise AppError(
            "The market data provider took too long to answer.",
            status_code=504,
            code="upstream_timeout",
        ) from exc
    except httpx.HTTPError as exc:
        logger.warning("Market provider transport error: %s", type(exc).__name__)
        raise UpstreamError("Could not reach the market data provider.") from exc

    if response.status_code == 429:
        # Worth its own message: on a free tier this is the common failure, and
        # "try again in a moment" is genuinely the right advice.
        raise AppError(
            "Market data is rate limited right now. Try again shortly.",
            status_code=429,
            code="rate_limited",
        )

    if response.status_code in (401, 403):
        # The key is wrong or lacks the plan for this data. That is an
        # operator problem, so it goes to the log in full and reaches the
        # trader as something that is plainly not their fault.
        logger.error("Market provider rejected the API key: %s", response.status_code)
        raise AppError(
            "Market data is not available on this deployment's plan.",
            status_code=503,
            code="market_unconfigured",
        )

    if response.status_code >= 400:
        logger.warning("Market provider returned %s", response.status_code)
        raise UpstreamError("Could not load market data.")

    try:
        parsed = _parse(response.json())
    except ValueError as exc:
        logger.warning("Market provider sent a body that was not JSON")
        raise UpstreamError("Could not read the market data response.") from exc

    trimmed = parsed[:MAX_BARS]
    _cache_put(key, trimmed)
    return trimmed, span
