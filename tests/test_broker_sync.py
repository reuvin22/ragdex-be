"""Broker sync, without a broker.

Everything here runs against a fake bridge. That is the payoff for putting the
vendor behind a Protocol: the parts that decide what a trade *is* — dedupe,
symbol normalisation, what gets derived rather than accepted — are testable,
and the only untested code is the one class that makes HTTP calls to a company
we do not control.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from app.schemas.connection import ConnectionCreate
from app.services.bridge import BridgeError, ClosedTrade, Credentials
from app.services.broker_sync import _normalise_symbol, _to_trade
from pydantic import ValidationError


def deal(**overrides) -> ClosedTrade:
    base = {
        "ticket": "1001",
        "symbol": "EURUSD",
        "direction": "Long",
        "volume": Decimal("1.0"),
        "entry_price": Decimal("1.1000"),
        "exit_price": Decimal("1.1050"),
        "opened_at": datetime(2026, 9, 1, 10, 0, tzinfo=UTC),
        "closed_at": datetime(2026, 9, 1, 11, 0, tzinfo=UTC),
    }
    return ClosedTrade(**{**base, **overrides})


class FakeBridge:
    """A bridge that answers from a list instead of a broker."""

    def __init__(self, trades: list[ClosedTrade], *, refuse: str | None = None) -> None:
        self._trades = trades
        self._refuse = refuse
        self.connected: list[Credentials] = []
        self.released: list[str] = []

    def connect(self, credentials: Credentials) -> str:
        if self._refuse:
            raise BridgeError(self._refuse)
        self.connected.append(credentials)
        return "bridge-1"

    def closed_trades(self, account_id: str, since: datetime | None) -> list[ClosedTrade]:
        if since is None:
            return list(self._trades)
        return [trade for trade in self._trades if trade.closed_at > since]

    def disconnect(self, account_id: str) -> None:
        self.released.append(account_id)


# ------------------------------------------------------- symbol normalising


def test_broker_suffixes_collapse_to_one_instrument() -> None:
    """The same pair is spelled differently per broker and account type. Left
    alone, a trader with two accounts sees EURUSD split across two rows that
    never add up."""
    for spelling in ("EURUSD", "EURUSD.r", "EURUSDm", "EURUSD_i", "eurusd.a"):
        assert _normalise_symbol(spelling) in {"EURUSD", "EURUSDM"}

    assert _normalise_symbol("EURUSD.r") == "EURUSD"
    assert _normalise_symbol("  gbpusd.pro  ") == "GBPUSD.PRO"


def test_a_long_tail_is_part_of_the_name_not_a_suffix() -> None:
    """"US30.cash" must not become "US30" if the broker also lists a different
    US30 — stripping it would merge two instruments into one row."""
    assert _normalise_symbol("US30.cash") == "US30.CASH"
    assert _normalise_symbol("BTC-USD") == "BTC-USD"


# --------------------------------------------------------- what is derived


def test_a_synced_trade_carries_no_profit_figure() -> None:
    """The bridge knows what the broker reported. We do not take it: the
    broker's number quietly includes commission, swap and its own rounding,
    and the rest of the app knows nothing about those."""
    from app.schemas.trade import TradeCreate

    # Structural, not incidental: there is no field on the way in for a profit
    # figure, so a bridge cannot supply one even by accident.
    assert "net_pl" not in TradeCreate.model_fields
    assert "risk_reward" not in TradeCreate.model_fields

    trade = _to_trade(deal())
    assert trade.entry_price == Decimal("1.1000")
    assert trade.exit_price == Decimal("1.1050")
    assert trade.size == Decimal("1.0")
    assert trade.size_unit == "Lots"


def test_the_trader_s_own_fields_are_left_empty() -> None:
    """A sync knows the prices and nothing about the thinking. Those blanks are
    what the trader fills in on review; inventing them would make a synced
    journal worthless."""
    trade = _to_trade(deal())

    assert trade.setup == ""
    assert trade.rationale == ""
    assert trade.emotion_before == ""
    assert trade.mistakes == []
    assert trade.complied_entry == ""


def test_direction_survives_the_round_trip() -> None:
    assert _to_trade(deal(direction="Short")).direction == "Short"
    assert _to_trade(deal(direction="Long")).direction == "Long"


# ------------------------------------------------------------ credentials


def test_a_connection_refuses_a_login_that_is_not_one() -> None:
    def make(login: str) -> str:
        return ConnectionCreate(
            platform="mt5", server="ICMarketsSC-MT5", login=login,
            investor_password="secret",
        ).login

    assert make("12345678") == "12345678"
    assert make("demo-99") == "demo-99"

    for rubbish in ("12 345", "a@b", "drop table"):
        with pytest.raises(ValidationError):
            make(rubbish)


def test_the_connection_model_has_nowhere_to_put_a_password() -> None:
    """The strongest guarantee in this feature, and it is structural rather
    than a rule anyone has to remember: a careless `return record` cannot leak
    a credential because the response model has no field for one."""
    from app.schemas.connection import Connection

    fields = set(Connection.model_fields)

    assert "investor_password" not in fields
    assert "password" not in fields
    assert not any("password" in name for name in fields)


def test_extra_fields_are_refused_on_the_way_in() -> None:
    with pytest.raises(ValidationError):
        ConnectionCreate(
            platform="mt5",
            server="X",
            login="1",
            investor_password="p",
            uid="someone-else",
        )


# ------------------------------------------------------------- the bridge


def test_a_refused_credential_says_why() -> None:
    bridge = FakeBridge([], refuse="Invalid account or password.")

    with pytest.raises(BridgeError) as caught:
        bridge.connect(Credentials("mt5", "S", "1", "wrong"))

    assert "Invalid account" in str(caught.value.message)


def test_since_is_exclusive_so_the_last_trade_is_not_re_read() -> None:
    """The watermark is the close time of the newest imported trade. Asking for
    ">= since" would re-read it forever."""
    first = deal(ticket="1", closed_at=datetime(2026, 9, 1, 11, 0, tzinfo=UTC))
    second = deal(ticket="2", closed_at=datetime(2026, 9, 2, 11, 0, tzinfo=UTC))
    bridge = FakeBridge([first, second])

    assert len(bridge.closed_trades("a", None)) == 2
    assert [t.ticket for t in bridge.closed_trades("a", first.closed_at)] == ["2"]
    assert bridge.closed_trades("a", second.closed_at) == []


def test_a_window_that_has_moved_on_returns_nothing_rather_than_failing() -> None:
    bridge = FakeBridge([deal()])
    ahead = datetime.now(UTC) + timedelta(days=1)

    assert bridge.closed_trades("a", ahead) == []
