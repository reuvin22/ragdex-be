"""Market candle payloads.

Nothing here is per-user. These are public market facts, and the response is
the same for every trader who asks the same question — which is why the route
that serves them still requires a session but never consults a uid.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

#: Bar sizes the service is willing to serve. Mirrors `_SPANS` in
#: services/market.py; the client only ever reads this back, never asks for it.
Span = Literal["1m", "5m", "15m", "1h", "1d"]

#: Longest window a single request may cover, in days. A trade open for years
#: is a position, not something this chart is for, and the cap keeps one
#: request from becoming a very large provider bill.
MAX_WINDOW_DAYS = 400


class Candle(BaseModel):
    """One bar, with the time in epoch milliseconds as the provider sent it."""

    model_config = ConfigDict(extra="forbid")

    time: int
    open: float
    high: float
    low: float
    close: float
    volume: float


class CandlesResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ticker: str
    #: Chosen from the window's length by the service, not by the caller.
    span: Span
    candles: list[Candle] = Field(default_factory=list)
    #: True when the provider simply has no bars for this symbol and window —
    #: a delisted ticker, a symbol it does not carry, a window that is entirely
    #: outside market hours. Distinct from an error, and the client says so.
    empty: bool = False
