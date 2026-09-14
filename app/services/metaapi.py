"""One bridge implementation, talking to MetaApi.

**Untested against the real service.** Everything else in this feature runs
against a fake; this is the part that cannot, because testing it means holding
a live brokerage account. Treat the request shapes below as read from
documentation rather than proven, and expect to correct them the first time a
real account connects.

Written with no SDK. MetaApi publishes one, but it pulls in a websocket stack
and a dependency tree far larger than the four HTTP calls actually needed here,
and this service already makes its outbound calls with httpx — the same choice,
and the same reasoning, as signing R2 URLs by hand rather than adding boto3.
"""

from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from app.core.config import Settings
from app.services.bridge import BridgeError, ClosedTrade, Credentials

logger = logging.getLogger(__name__)

#: Generous, because provisioning an account is slow by nature — a terminal is
#: being started on the other end.
_TIMEOUT = httpx.Timeout(30.0, connect=10.0)

#: What a failed login looks like, in words a trader can act on. The vendor's
#: own messages talk about provisioning profiles and deployment states, which
#: tell the trader nothing about the password they typed.
_REFUSED = (
    "Your broker refused those details. Check the server name and that you "
    "used the investor password, not your trading password."
)


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _moment(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        # ISO-8601 with a trailing Z, which fromisoformat only learned in 3.11.
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


class MetaApiBridge:
    """Reads closed positions. Deliberately has no method that can trade."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        token = settings.bridge_token
        self._headers = {
            "auth-token": token.get_secret_value() if token else "",
            "Content-Type": "application/json",
        }

    # ------------------------------------------------------------ helpers

    def _request(self, method: str, url: str, **kwargs: Any) -> Any:
        try:
            with httpx.Client(timeout=_TIMEOUT) as client:
                response = client.request(method, url, headers=self._headers, **kwargs)
        except httpx.HTTPError as error:
            logger.warning("bridge unreachable: %s", error)
            raise BridgeError(
                "Could not reach the sync service. Try again shortly."
            ) from error

        if response.status_code in (400, 401, 403):
            # The trader's credentials, or ours. Logged without the body: it
            # can echo back what was sent, and what was sent is a password.
            logger.warning("bridge refused: %s %s", response.status_code, url)
            raise BridgeError(_REFUSED)

        if response.status_code >= 400:
            logger.warning("bridge error: %s %s", response.status_code, url)
            raise BridgeError("The sync service had a problem. Try again shortly.")

        return response.json() if response.content else None

    # -------------------------------------------------------------- bridge

    def connect(self, credentials: Credentials) -> str:
        """Provision an account and return the bridge's id for it."""
        body = {
            "login": credentials.login,
            "password": credentials.password,
            "name": f"ragdex-{credentials.login}",
            "server": credentials.server,
            "platform": credentials.platform,
            # The magic number is what MetaTrader stamps on orders this
            # connection places. Zero, because it places none.
            "magic": 0,
            "region": "new-york",
        }

        payload = self._request(
            "POST",
            f"{self._settings.bridge_provisioning_url}/users/current/accounts",
            json=body,
        )

        account_id = (payload or {}).get("id")
        if not account_id:
            raise BridgeError("The sync service did not return an account.")

        return str(account_id)

    def closed_trades(
        self, account_id: str, since: datetime | None
    ) -> list[ClosedTrade]:
        start = (since or datetime(1970, 1, 1)).isoformat()
        end = datetime.now(tz=(since.tzinfo if since else None)).isoformat()

        payload = self._request(
            "GET",
            f"{self._settings.bridge_base_url}/users/current/accounts/"
            f"{account_id}/history-deals/time/{start}/{end}",
        )

        return self._pair(payload or [])

    def disconnect(self, account_id: str) -> None:
        """Release the account so it stops being billed.

        A failure here is swallowed. Disconnecting must always succeed from the
        trader's side — leaving them unable to remove an account because a
        third party is down would be the worse outcome, and an orphaned account
        is a billing question rather than a user-facing one.
        """
        try:
            self._request(
                "DELETE",
                f"{self._settings.bridge_provisioning_url}/users/current/"
                f"accounts/{account_id}",
            )
        except BridgeError:
            logger.warning("could not release bridge account %s", account_id)

    # --------------------------------------------------------------- deals

    def _pair(self, deals: list[dict[str, Any]]) -> list[ClosedTrade]:
        """Turn a list of deals into round trips.

        The part of this integration most likely to be wrong, and the part
        worth understanding before changing: MetaTrader 5 does not record
        trades, it records *deals*. Opening a position is one deal and closing
        it is another, linked by a position id. Read naively, every trade
        appears twice — once as a purchase with no result and once as a sale at
        a price that looks like an entry.

        So deals are grouped by position, and only positions with both halves
        become trades. An open position has no result to journal yet, and will
        be picked up by a later sync once it closes.
        """
        positions: dict[str, dict[str, dict[str, Any]]] = {}

        for deal in deals:
            position_id = str(deal.get("positionId") or "")
            entry = deal.get("entryType")

            if not position_id or entry not in ("DEAL_ENTRY_IN", "DEAL_ENTRY_OUT"):
                continue

            positions.setdefault(position_id, {})[entry] = deal

        trades: list[ClosedTrade] = []

        for position_id, halves in positions.items():
            opened = halves.get("DEAL_ENTRY_IN")
            closed = halves.get("DEAL_ENTRY_OUT")
            if opened is None or closed is None:
                continue

            entry_price = _decimal(opened.get("price"))
            exit_price = _decimal(closed.get("price"))
            volume = _decimal(closed.get("volume")) or _decimal(opened.get("volume"))
            opened_at = _moment(opened.get("time"))
            closed_at = _moment(closed.get("time"))

            if None in (entry_price, exit_price, volume, opened_at, closed_at):
                # Not an error worth failing the batch for — one unreadable
                # deal among hundreds. It is simply not a trade we can state.
                logger.info("skipping unreadable position %s", position_id)
                continue

            # The *opening* deal says which way the position went. The closing
            # deal is always the opposite, so reading direction from it would
            # invert every trade in the journal.
            direction = "Long" if opened.get("type") == "DEAL_TYPE_BUY" else "Short"

            trades.append(
                ClosedTrade(
                    # The position id, not a deal id: it is the thing that
                    # identifies the round trip, and it is what dedupe keys on.
                    ticket=position_id,
                    symbol=str(closed.get("symbol") or opened.get("symbol") or ""),
                    direction=direction,
                    volume=volume,  # type: ignore[arg-type]
                    entry_price=entry_price,  # type: ignore[arg-type]
                    exit_price=exit_price,  # type: ignore[arg-type]
                    opened_at=opened_at,  # type: ignore[arg-type]
                    closed_at=closed_at,  # type: ignore[arg-type]
                    stop_loss=_decimal(opened.get("stopLoss")),
                    take_profit=_decimal(opened.get("takeProfit")),
                )
            )

        trades.sort(key=lambda trade: trade.closed_at)
        return trades
