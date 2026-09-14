"""A bridge implementation for the self-hosted mt5-bridge.

https://github.com/mobjoy0/mt5-bridge — a Node service in front of an MQL5
Expert Advisor running inside a real MetaTrader 5 terminal. No subscription:
the cost is a terminal you run yourself.

It differs from a hosted bridge in three ways that shape this whole file.

**One instance is one account.** Nothing in its API names an account —
``/history/orders`` and ``/account`` answer for whichever terminal that
instance is logged into. So a RagDex deployment serving many traders needs one
instance per connected account, and the URL for it belongs to the *connection*
rather than to the service. Until that plumbing exists, this reads the single
instance named in settings and **refuses to import unless the account number it
reports matches the one the trader entered** — see ``connect``. Importing a
stranger's trades into someone's journal is the one failure here that cannot be
undone by fixing a bug later.

**It has no authentication.** `app.use(cors())` with no options, no API key, no
token. Anything that can reach the port can read the account — and, because the
same service exposes ``POST /order`` and ``POST /order/close``, can also trade
it. This must never be reachable from the internet. Bind it to loopback or a
private network, and put RagDex on the same side of that boundary.

**The EA has already paired the deals.** Asking for ``mode=positions`` returns
round trips, with the entry and exit halves joined by position id inside MQL5.
That is why there is no pairing code here and a great deal of it in the MetaApi
implementation next door.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from app.core.config import Settings
from app.services.bridge import BridgeError, ClosedTrade, Credentials

logger = logging.getLogger(__name__)

#: Short. The bridge is meant to be on the same network, and a terminal that is
#: not answering promptly is not answering.
_TIMEOUT = httpx.Timeout(20.0, connect=5.0)

#: A price of zero means "not set" in MetaTrader, not "at zero". Stops and
#: targets come back as 0.00000 when the trade had none.
_UNSET = Decimal("0")


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _price(value: Any) -> Decimal | None:
    """A price, or None where MetaTrader means "none"."""
    parsed = _decimal(value)
    return None if parsed is None or parsed == _UNSET else parsed


def _moment(value: Any) -> datetime | None:
    """A MetaTrader timestamp, read as UTC.

    The EA prints ``datetime`` with ``%d``, which gives Unix seconds — but in
    the *broker's server* time zone, not UTC. Most forex servers sit on
    UTC+2/+3, so a trade can land two or three hours from where it belongs.

    Read as UTC here deliberately. Getting it truly right needs the server's
    offset, which this bridge does not report, and a wrong offset applied
    confidently is worse than a known constant one: the session and
    time-of-day breakdowns in Analytics would be silently skewed with nothing
    to show for it. Correct this once the offset is available.
    """
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(int(value), tz=UTC)
    except (ValueError, OSError, OverflowError):
        return None


class Mt5BridgeAdapter:
    """Reads closed positions from a self-hosted mt5-bridge instance."""

    def __init__(self, settings: Settings) -> None:
        self._base = settings.bridge_base_url.rstrip("/")

    # ------------------------------------------------------------ requests

    def _get(self, path: str, **params: Any) -> Any:
        try:
            with httpx.Client(timeout=_TIMEOUT) as client:
                response = client.get(f"{self._base}{path}", params=params)
        except httpx.HTTPError as error:
            logger.warning("mt5-bridge unreachable at %s: %s", self._base, error)
            raise BridgeError(
                "Could not reach the MetaTrader bridge. Check that the "
                "terminal is running and the Expert Advisor is attached."
            ) from error

        if response.status_code >= 400:
            logger.warning("mt5-bridge error %s on %s", response.status_code, path)
            raise BridgeError(
                "The MetaTrader bridge could not answer. Check the terminal."
            )

        try:
            return response.json()
        except ValueError as error:
            # The EA builds JSON with StringFormat, so a symbol or comment
            # holding a quote produces output that is not valid JSON. Worth
            # naming rather than surfacing as a generic failure.
            raise BridgeError(
                "The bridge returned something unreadable."
            ) from error

    # -------------------------------------------------------------- bridge

    def connect(self, credentials: Credentials) -> str:
        """Confirm the terminal is logged into the account the trader named.

        Nothing is provisioned — the terminal is already running and already
        logged in. So "connecting" here means *verifying*, and the check is
        the point of the method.

        A hosted bridge is handed credentials and returns an account scoped to
        them. This one answers for whatever terminal it happens to be running,
        which means that without this comparison a trader could type any
        account number and receive somebody else's trades. Refusing on a
        mismatch is the only thing standing between that and a journal quietly
        filled with a stranger's history.
        """
        if credentials.platform != "mt5":
            raise BridgeError(
                "This bridge only supports MetaTrader 5 accounts."
            )

        account = self._get("/account") or {}
        reported = str(account.get("login") or "").strip()

        if not reported:
            raise BridgeError(
                "The bridge did not say which account it is logged into."
            )

        if reported != credentials.login.strip():
            logger.warning(
                "refusing connection: bridge serves %s, trader asked for %s",
                reported,
                credentials.login,
            )
            raise BridgeError(
                f"That bridge is logged into account {reported}, not "
                f"{credentials.login}. Point RagDex at the right terminal, or "
                "connect the account it is actually running."
            )

        # The account number is the id: there is nothing else to identify an
        # instance by, and it is what the dedupe key is built from.
        return reported

    def closed_trades(
        self, account_id: str, since: datetime | None
    ) -> list[ClosedTrade]:
        start = since or datetime.now(UTC) - timedelta(days=730)

        rows = self._get(
            "/history/orders",
            mode="positions",
            # Whole days, which is all the endpoint accepts. A day is re-read
            # on every sync as a result — harmless, because importing is keyed
            # on the position ticket and a repeat writes nothing.
            from_date=start.strftime("%Y-%m-%d"),
            to_date=(datetime.now(UTC) + timedelta(days=1)).strftime("%Y-%m-%d"),
        )

        if not isinstance(rows, list):
            # Some responses wrap the array; take it wherever it is.
            rows = (rows or {}).get("data") or (rows or {}).get("positions") or []

        return self._to_trades(rows, since)

    def disconnect(self, account_id: str) -> None:
        """Nothing to release.

        The terminal keeps running whatever RagDex thinks — it is not ours to
        stop, and stopping it would break whoever else is watching that
        account. Unlike a hosted bridge there is no per-account billing to end.
        """
        return None

    # --------------------------------------------------------------- deals

    def _to_trades(
        self, rows: list[dict[str, Any]], since: datetime | None
    ) -> list[ClosedTrade]:
        trades: list[ClosedTrade] = []

        for row in rows:
            ticket = str(row.get("ticket") or "").strip()
            opened_at = _moment(row.get("open_time"))
            closed_at = _moment(row.get("close_time"))
            entry_price = _price(row.get("open_price"))
            exit_price = _price(row.get("close_price"))
            volume = _decimal(row.get("volume"))

            if not ticket or None in (opened_at, closed_at, entry_price, exit_price):
                logger.info("skipping unreadable position %r", ticket or row)
                continue

            if volume is None or volume <= 0:
                continue

            # The endpoint is day-resolution, so the day of the last sync comes
            # back in full. Trimming here keeps the batch honest; the import is
            # idempotent regardless.
            if since is not None and closed_at is not None and closed_at <= since:
                continue

            trades.append(
                ClosedTrade(
                    ticket=ticket,
                    symbol=str(row.get("symbol") or "").strip(),
                    # "POSITION_TYPE_BUY" / "POSITION_TYPE_SELL", straight from
                    # EnumToString in the EA.
                    direction="Long"
                    if str(row.get("type", "")).endswith("BUY")
                    else "Short",
                    volume=volume,
                    entry_price=entry_price,  # type: ignore[arg-type]
                    exit_price=exit_price,  # type: ignore[arg-type]
                    opened_at=opened_at,  # type: ignore[arg-type]
                    closed_at=closed_at,  # type: ignore[arg-type]
                    stop_loss=_price(row.get("sl_price")),
                    take_profit=_price(row.get("tp_price")),
                )
            )

        trades.sort(key=lambda trade: trade.closed_at)
        return trades
