"""Talking to whoever runs the MetaTrader terminals.

MetaTrader has no cloud API. Reading an account without asking the trader to
install anything means a headless terminal has to run somewhere, logged in as
them — which is a real machine, with real CPU and memory, and is why every
vendor who does this charges per connected account per month.

So this file is an interface with one implementation behind it, rather than
vendor calls scattered through the sync service. Three reasons, in order of how
much they will matter:

1. **The vendor is a running cost that scales with users.** The day that bill
   justifies moving to a competitor — or to self-hosted terminals — the change
   should be one class, not a search through the codebase.
2. **It cannot be tested against.** Nobody is standing up a real broker account
   in CI. Everything around this is testable precisely because this is the only
   part that is not.
3. Vendors of this kind are small companies. Planning for the one you picked to
   disappear is not pessimism, it is arithmetic.

What every implementation must guarantee, whatever it talks to:

- **Read-only.** The credential handed over is the investor password and no
  implementation may expose an order-placing call. A journal does not trade.
- **Closed positions only.** An open position has no result to record yet.
- **Deals paired into round trips.** MT5 reports an entry deal and an exit deal
  sharing a position id; a caller must never see those as two trades.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from app.core.errors import AppError


class BridgeError(AppError):
    """The bridge could not do what was asked.

    Carries a message meant for the trader, because the common causes are
    theirs to fix: a wrong password, a server name that does not exist, an
    account the broker has disabled.
    """

    def __init__(self, message: str, *, status_code: int = 502) -> None:
        super().__init__(message, status_code=status_code, code="bridge_error")


@dataclass(frozen=True)
class Credentials:
    platform: str
    server: str
    login: str
    password: str


@dataclass(frozen=True)
class ClosedTrade:
    """One completed round trip, as the bridge saw it.

    Deliberately without P&L. The bridge knows what the broker reported, but
    this service derives money and R from prices and size the same way it does
    for a hand-typed trade — one arithmetic path, so a synced trade and a typed
    one cannot disagree about what a 2R winner is.
    """

    #: The broker's own id for the round trip. The deduplication key: a sync
    #: that runs twice must not file the same trade twice.
    ticket: str
    symbol: str
    #: "Long" or "Short", already normalised from whatever the platform calls
    #: buy and sell.
    direction: str
    volume: Decimal
    entry_price: Decimal
    exit_price: Decimal
    opened_at: datetime
    closed_at: datetime
    stop_loss: Decimal | None = None
    take_profit: Decimal | None = None


class Bridge(Protocol):
    """What the sync service needs, and nothing more."""

    def connect(self, credentials: Credentials) -> str:
        """Stand an account up and return the bridge's id for it.

        Raises BridgeError with something the trader can act on when the
        credentials are refused.
        """
        ...

    def closed_trades(self, account_id: str, since: datetime | None) -> list[ClosedTrade]:
        """Round trips closed after ``since``, oldest first.

        ``since`` is exclusive, so passing the last imported close time asks
        for strictly newer trades. None means everything the broker keeps.
        """
        ...

    def disconnect(self, account_id: str) -> None:
        """Release the account, and stop being billed for it.

        Called on delete. Worth getting right: a bridge account left running
        after a trader disconnects is a bill for a feature nobody is using.
        """
        ...
