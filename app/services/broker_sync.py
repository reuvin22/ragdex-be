"""Turning what a broker reports into journal entries.

The part of broker sync with no vendor in it, which is why it is the part with
tests. Everything that talks to the outside world is behind ``Bridge``; this
decides what a round trip means once it arrives.

Two rules shape the whole file:

**Nothing computed is accepted.** The bridge reports prices, size and times.
P&L and R are derived here by the same code that derives them for a trade typed
in by hand, so a synced trade and a typed one cannot disagree about what a 2R
winner is. A broker's own profit figure would be easier and would quietly
include commission, swap and the broker's rounding — three things the rest of
the app knows nothing about.

**Importing twice is normal.** A sync re-reads overlapping history every time
it runs. The write is keyed on the broker's ticket, so a repeat is a no-op
rather than a duplicate trade.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.repositories import connections as repo
from app.repositories.trades import import_trade
from app.schemas.trade import TradeCreate
from app.services.bridge import Bridge, BridgeError, ClosedTrade, Credentials

logger = logging.getLogger(__name__)

#: How far back a first sync reaches.
#:
#: Two years, not everything. A trader with a decade of history would otherwise
#: import thousands of trades on first connect — most of them from strategies
#: they no longer run — and every statistic in the app would describe someone
#: they used to be.
FIRST_SYNC_DAYS = 730

#: Ceiling on one pass, so a large history arrives over several syncs rather
#: than one request that times out on a free-tier instance.
MAX_PER_SYNC = 500


@dataclass(frozen=True)
class SyncResult:
    added: int
    seen: int
    through: datetime | None


def _to_trade(deal: ClosedTrade) -> TradeCreate:
    """One round trip, in the shape the journal already speaks.

    Times are what the platform reported. Everything the trader would add
    themselves — setup, rationale, how they felt, whether they followed the
    plan — is left empty, because a sync knows none of it. Those blanks are the
    point: they are what the trader fills in when reviewing, and a synced
    journal with them invented would be worthless.
    """
    return TradeCreate(
        ticker=_normalise_symbol(deal.symbol),
        direction="Long" if deal.direction == "Long" else "Short",
        size=deal.volume,
        size_unit="Lots",
        entry_price=deal.entry_price,
        exit_price=deal.exit_price,
        entry_at=deal.opened_at,
        exit_at=deal.closed_at,
        stop_loss=deal.stop_loss,
        take_profit=deal.take_profit,
    )


def _normalise_symbol(symbol: str) -> str:
    """Strip the suffix brokers add to the same instrument.

    EURUSD is filed as "EURUSD.r", "EURUSDm", "EURUSD_i" and a dozen other
    spellings depending on the broker and the account type. Left alone, a
    trader with two accounts sees their EURUSD performance split across two
    rows that never add up.

    Only a trailing suffix after a separator, and only when what is left still
    looks like a symbol — "US30.cash" must not become "US30" if that costs the
    distinction between two genuinely different instruments at the same broker.
    """
    cleaned = symbol.strip().upper()

    for separator in (".", "_", "-"):
        head, found, tail = cleaned.partition(separator)
        # A short tail is a broker suffix; a long one is part of the name.
        if found and len(head) >= 6 and len(tail) <= 2:
            return head

    return cleaned


def sync_connection(uid: str, connection_id: str, bridge: Bridge) -> SyncResult:
    """Bring one connected account up to date.

    Returns what changed rather than raising on an empty pass: "nothing new"
    is the normal outcome, and the common case should not look like an error.
    """
    credentials = repo.credentials_for(uid, connection_id)
    if credentials is None:
        raise BridgeError("That connection no longer exists.", status_code=404)

    account_id = credentials["bridge_account_id"]

    if account_id is None:
        # First run: stand the account up, and remember the id so the next
        # sync does not pay to create it again.
        try:
            account_id = bridge.connect(
                Credentials(
                    platform=credentials["platform"],
                    server=credentials["server"],
                    login=credentials["login"],
                    password=credentials["password"],
                )
            )
        except BridgeError as error:
            # The message is the trader's to act on — a wrong password, a
            # server that does not exist — so it is stored, not just logged.
            repo.mark_state(uid, connection_id, "failed", message=str(error.message))
            raise

        repo.mark_state(uid, connection_id, "connected", bridge_account_id=account_id)

    since = credentials["synced_through"]
    if since is None:
        # A timedelta, not year arithmetic: replacing the year on 29 February
        # raises, which would break a first sync one day in every four.
        since = datetime.now(UTC) - timedelta(days=FIRST_SYNC_DAYS)

    deals = bridge.closed_trades(account_id, since)[:MAX_PER_SYNC]

    added = 0
    newest: datetime | None = None

    for deal in deals:
        try:
            if import_trade(uid, _to_trade(deal), source_id=f"{account_id}_{deal.ticket}"):
                added += 1
        except Exception:
            # One malformed deal must not abandon the rest of the batch. A
            # broker sending a zero price or a close before an open is rare,
            # unfixable from here, and not worth losing the other 499 trades.
            logger.warning(
                "skipped deal %s on connection %s", deal.ticket, connection_id,
                exc_info=True,
            )
            continue

        if newest is None or deal.closed_at > newest:
            newest = deal.closed_at

    if newest is not None:
        repo.mark_synced(uid, connection_id, through=newest, added=added)

    return SyncResult(added=added, seen=len(deals), through=newest)
