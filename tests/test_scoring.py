"""Points, and what they must not reveal.

The arena publishes a rank beside a name to every other account. That is only
defensible while a rank cannot be read backwards into somebody's finances, so
most of this file is about what the score does *not* depend on.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import pairwise

import pytest
from app.models.schemas.competition import DIVISIONS
from app.models.schemas.trade import Trade
from app.services.scoring import DAILY_TRADE_CAP, rank_for, score

DAY = datetime(2026, 9, 21, 10, 0, tzinfo=UTC)


def trade(
    *,
    pl: str | None = "100",
    entry: bool | None = True,
    exit_: bool | None = True,
    management: bool | None = True,
    at: datetime = DAY,
) -> Trade:
    def answer(value: bool | None) -> str:
        return "" if value is None else ("yes" if value else "no")

    return Trade(
        id="t",
        ticker="NAS100",
        direction="Long",
        entry_at=at,
        net_pl=None if pl is None else Decimal(pl),
        complied_entry=answer(entry),
        complied_exit=answer(exit_),
        complied_management=answer(management),
    )


# ------------------------------------------------- what points must not be


@pytest.mark.parametrize("amount", ["1", "100", "10000", "1000000"])
def test_the_size_of_a_win_does_not_change_the_score(amount):
    """The whole privacy argument.

    If points rose with P&L, a public leaderboard would be a public ranking of
    account sizes. A trade is read as won or lost — never as an amount.
    """
    baseline, _ = score([trade(pl="100")])
    assert score([trade(pl=amount)])[0] == baseline


def test_the_size_of_a_loss_does_not_change_the_score():
    small, _ = score([trade(pl="-1")])
    large, _ = score([trade(pl="-999999")])

    assert small == large


def test_two_traders_with_the_same_discipline_score_the_same():
    """A fifty-pound account and a fifty-thousand-pound account, same habits."""
    modest = [trade(pl="20"), trade(pl="-10", at=DAY + timedelta(days=1))]
    large = [trade(pl="20000"), trade(pl="-10000", at=DAY + timedelta(days=1))]

    assert score(modest)[0] == score(large)[0]


# ----------------------------------------------------- what points reward


def test_keeping_the_rules_beats_breaking_them():
    kept, _ = score([trade(entry=True, exit_=True, management=True)])
    broken, _ = score([trade(entry=False, exit_=True, management=True)])

    assert kept > broken


def test_a_break_costs_more_than_a_keep_earns():
    """The habit is what is scored, so a lapse has to outweigh a success."""
    _, parts = score([trade(entry=False, exit_=False, management=False)])

    assert parts["Rules broken"] < 0
    assert parts["Days with a break"] < 0


def test_an_ungraded_trade_earns_nothing():
    """A blank is "not graded", not "followed" — the same rule the discipline
    score already follows."""
    points, parts = score([trade(entry=None, exit_=None, management=None)])

    assert points == 0
    assert parts == {}


def test_a_clean_day_is_worth_more_than_the_trades_on_it():
    one_clean_day, _ = score([trade(), trade()])
    two_clean_days, _ = score([trade(), trade(at=DAY + timedelta(days=1))])

    # Same trades, spread over two days: two day bonuses rather than one.
    assert two_clean_days > one_clean_day


def test_volume_is_capped():
    """Otherwise the quickest way up the ladder is forty positions a day."""
    at_cap = [trade(at=DAY) for _ in range(DAILY_TRADE_CAP)]
    well_over = [trade(at=DAY) for _ in range(DAILY_TRADE_CAP * 4)]

    assert score(at_cap)[0] == score(well_over)[0]


def test_points_never_go_negative():
    """A bad month should cost a rank, not make the ladder unrecoverable."""
    disaster = [
        trade(
            entry=False,
            exit_=False,
            management=False,
            pl="-500",
            at=DAY + timedelta(days=n),
        )
        for n in range(20)
    ]

    assert score(disaster)[0] == 0


def test_the_breakdown_accounts_for_the_score():
    """A score nobody can account for is a score nobody trusts."""
    points, parts = score([trade(), trade(entry=False, at=DAY + timedelta(days=1))])

    assert points == max(0, sum(parts.values()))


# ----------------------------------------------------------- the window


def test_a_window_scores_only_what_is_inside_it():
    """What makes a tournament the ladder's scoring over a fortnight."""
    inside = trade(at=DAY)
    outside = trade(at=DAY - timedelta(days=60))

    whole, _ = score([inside, outside])
    windowed, _ = score([inside, outside], since=DAY - timedelta(days=7))

    assert windowed < whole
    assert windowed == score([inside])[0]


# ------------------------------------------------------------- the ladder


def test_the_ladder_is_ordered_and_gaps_grow():
    """Climbing out of Bronze should be quicker than climbing out of Diamond."""
    thresholds = [threshold for _, _, threshold in DIVISIONS]

    assert thresholds == sorted(thresholds)

    steps = [b - a for a, b in pairwise(thresholds)]
    assert steps[0] < steps[-1]


def test_twelve_hundred_points_is_gold_two():
    """The example this ladder was built to match."""
    rank = rank_for(1200)

    assert rank.tier == "Gold"
    assert rank.division == 2


def test_a_new_account_starts_at_the_bottom():
    rank = rank_for(0)

    assert rank.tier == "Bronze"
    assert rank.division == 1
    assert rank.next_at == 120


def test_the_top_of_the_ladder_has_nowhere_to_go():
    assert rank_for(99_999).tier == "Master"
    assert rank_for(99_999).next_at is None


@pytest.mark.parametrize("points", [0, 119, 120, 999, 1000, 1199, 1200, 3519, 3520])
def test_every_score_lands_somewhere(points):
    """No gap in the ladder, and no score without a badge."""
    rank = rank_for(points)

    assert rank.tier in {name for name, _, _ in DIVISIONS}
    assert rank.division in (1, 2, 3)
    assert rank.points == points
