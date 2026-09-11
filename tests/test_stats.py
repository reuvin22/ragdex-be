"""The arithmetic the coach quotes.

Pure functions, no mocking — these are the tests worth having, because every
statistic the product shows is built on them.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.schemas.trade import Trade
from app.services.stats import summarise

BASE = datetime(2026, 3, 2, 9, 30, tzinfo=UTC)


def trade(**overrides) -> Trade:
    defaults = dict(
        id="t",
        ticker="NVDA",
        direction="Long",
        size=Decimal("100"),
        entry_at=BASE,
        exit_at=BASE + timedelta(minutes=30),
        created_at=BASE,
        net_pl=Decimal("100"),
    )
    return Trade(**{**defaults, **overrides})


def test_empty_journal_is_all_zeros() -> None:
    summary = summarise([])
    assert summary.trade_count == 0
    assert summary.net_pl == 0
    assert summary.profit_factor is None


def test_win_rate_and_net_pl() -> None:
    summary = summarise(
        [
            trade(id="a", net_pl=Decimal("300")),
            trade(id="b", net_pl=Decimal("-100")),
            trade(id="c", net_pl=Decimal("200")),
        ]
    )

    assert summary.closed_count == 3
    assert summary.net_pl == 400
    assert round(summary.win_rate) == 67
    assert summary.avg_win == 250
    assert summary.avg_loss == 100


def test_profit_factor_is_none_without_losses() -> None:
    """Dividing by a zero gross loss would be infinity, which is not a fact
    worth telling a trader."""
    summary = summarise([trade(id="a"), trade(id="b")])
    assert summary.profit_factor is None


def test_open_trades_are_excluded_from_results() -> None:
    summary = summarise(
        [trade(id="a", net_pl=Decimal("100")), trade(id="b", net_pl=None)]
    )
    assert summary.trade_count == 2
    assert summary.closed_count == 1
    assert summary.net_pl == 100


def test_worst_losing_streak_is_consecutive_only() -> None:
    summary = summarise(
        [
            trade(id="a", net_pl=Decimal("-10"), entry_at=BASE),
            trade(id="b", net_pl=Decimal("-10"), entry_at=BASE + timedelta(minutes=10)),
            trade(id="c", net_pl=Decimal("50"), entry_at=BASE + timedelta(minutes=20)),
            trade(id="d", net_pl=Decimal("-10"), entry_at=BASE + timedelta(minutes=30)),
        ]
    )
    assert summary.worst_streak == 2


def test_after_loss_measures_the_next_trade() -> None:
    summary = summarise(
        [
            trade(id="a", net_pl=Decimal("-100"), entry_at=BASE),
            trade(id="b", net_pl=Decimal("-50"), entry_at=BASE + timedelta(minutes=5)),
        ]
    )

    assert summary.after_loss.count == 1
    assert summary.after_loss.net_pl == -50
    assert summary.after_loss.win_rate == 0
    assert summary.after_loss.median_minutes_to_reentry == 5.0


def test_overnight_gap_is_not_counted_as_reentry() -> None:
    """A trade the next morning says nothing about tilt."""
    summary = summarise(
        [
            trade(id="a", net_pl=Decimal("-100"), entry_at=BASE),
            # The exit has to move with the entry: the helper's default exit is
            # 30 minutes after BASE, which lands before an entry 20 hours later
            # and is rejected by the schema before summarise() ever sees it.
            trade(
                id="b",
                net_pl=Decimal("20"),
                entry_at=BASE + timedelta(hours=20),
                exit_at=BASE + timedelta(hours=20, minutes=30),
            ),
        ]
    )
    assert summary.after_loss.count == 1
    assert summary.after_loss.median_minutes_to_reentry is None


def test_setups_are_ranked_worst_first() -> None:
    summary = summarise(
        [
            trade(id="a", setup="Breakout", net_pl=Decimal("400")),
            trade(id="b", setup="Reversal", net_pl=Decimal("-300")),
        ]
    )
    assert summary.by_setup[0].label == "Reversal"


def test_compliance_ignores_unanswered_trades() -> None:
    summary = summarise(
        [
            trade(id="a", complied_entry="yes"),
            trade(id="b", complied_entry="no"),
            trade(id="c", complied_entry=""),
        ]
    )
    assert summary.plan_compliance["entry"] == 50
