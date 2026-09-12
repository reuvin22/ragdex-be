"""The trader's own limits, and the session a trade was taken in.

Both exist for the same reason: without them the coach can see that a position
was bigger than the last one but cannot say it broke anything, so "be more
disciplined" is all that is left — which is the advice this app exists to avoid
giving.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from app.repositories.trades import session_for
from app.schemas.profile import Profile
from app.schemas.trade import Trade
from app.services.coach import trading_setup
from app.services.stats import summarise


def _flat(text: str) -> str:
    return " ".join(text.split())


def _profile(**overrides) -> Profile:
    return Profile(uid="u1", currency="USD", **overrides)


# -- the trader's stated limits -------------------------------------------


def test_a_percentage_limit_is_also_shown_as_money() -> None:
    """The percentage is what they set; the cash figure is what they feel, and
    arithmetic is the one thing a small model reliably gets wrong."""
    setup = _flat(
        trading_setup(
            _profile(account_size=Decimal("50000"), risk_per_trade_pct=Decimal("1"))
        )
    )

    assert "maximum risk per trade: 1%" in setup
    assert "about USD 500 a trade" in setup


def test_a_prop_firm_is_named_and_its_stakes_spelled_out() -> None:
    setup = _flat(trading_setup(_profile(funding_type="prop_firm", prop_firm="FTMO")))

    assert "funded by FTMO" in setup
    assert "ends the account rather than denting it" in setup
    assert "never encourage them to trade through a limit" in setup


def test_a_demo_account_is_coached_but_not_congratulated() -> None:
    setup = _flat(trading_setup(_profile(funding_type="demo")))

    assert "habits are real even though the money is not" in setup


def test_an_empty_setup_says_so_rather_than_inventing_limits() -> None:
    """A coach that assumes a 1% limit nobody set is worse than one that asks."""
    setup = _flat(trading_setup(_profile()))

    assert "they have not filled this in" in setup
    assert "do not invent any of it" in setup


def test_the_limits_are_framed_as_the_trader_own_standard() -> None:
    setup = _flat(trading_setup(_profile(max_daily_loss_pct=Decimal("3"))))

    assert "as they described it themselves" in setup
    assert "broke a rule THEY wrote" in setup


def test_no_profile_contributes_nothing() -> None:
    assert trading_setup(None) == ""


# -- which session a trade belongs to --------------------------------------


def _at(hour: int) -> datetime:
    return datetime(2026, 9, 1, hour, 30, tzinfo=UTC)


def test_each_hour_lands_in_exactly_one_session() -> None:
    """The real sessions overlap; a trade happens once and needs one bucket."""
    assert {session_for(_at(hour)) for hour in range(24)} == {
        "asia",
        "london",
        "newyork",
    }


def test_the_bands_are_where_they_should_be() -> None:
    assert session_for(_at(2)) == "asia"
    assert session_for(_at(8)) == "london"
    assert session_for(_at(15)) == "newyork"
    assert session_for(_at(23)) == "asia"


def test_no_entry_time_means_no_session() -> None:
    """"No idea" with nothing to work from stays unknown rather than guessed."""
    assert session_for(None) == ""


def test_the_session_is_read_in_utc_whatever_the_offset() -> None:
    """A trader in Manila logging 20:00 local is trading the London session."""
    from datetime import timedelta, timezone

    manila = timezone(timedelta(hours=8))
    assert session_for(datetime(2026, 9, 1, 16, 0, tzinfo=manila)) == "london"


def test_sessions_are_bucketed_for_the_coach() -> None:
    trades = [
        Trade(
            id=f"t{index}",
            ticker="EURUSD",
            direction="Long",
            net_pl=Decimal("-200"),
            session="newyork",
        )
        for index in range(3)
    ] + [
        Trade(
            id="t9",
            ticker="EURUSD",
            direction="Long",
            net_pl=Decimal("400"),
            session="london",
        )
    ]

    by_session = summarise(trades).by_session

    # Worst first, and shown the way a trader says it.
    assert [bucket.label for bucket in by_session] == ["New York", "London"]
    assert by_session[0].net_pl == -600


def test_an_ungraded_session_is_left_out_of_the_buckets() -> None:
    trades = [Trade(id="t1", ticker="BTC", direction="Long", net_pl=Decimal("10"))]

    assert summarise(trades).by_session == []
