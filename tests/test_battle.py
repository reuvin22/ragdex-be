"""The battle: what a result is worth, and who can be paid for one.

The points table is the part worth pinning. Two of its three rows were
specified; the third was derived, and a derived rule is exactly the kind that
drifts silently.
"""

from __future__ import annotations

import pytest
from app.models.schemas.competition import BATTLE_POINTS
from app.services.competition import _relative
from app.services.scoring import rank_for

# Points that land squarely in a tier, so the tests read as brackets.
BRONZE = 50
SILVER = 500
GOLD = 1200
DIAMOND = 2600


def test_the_brackets_are_read_by_tier_not_by_points():
    """Gold I against Gold III is the same bracket.

    The table is about brackets, so two hundred points inside one tier must
    not change what a win is worth.
    """
    assert _relative(1000, 1420) == "same"
    assert rank_for(1000).tier == rank_for(1420).tier


@pytest.mark.parametrize(
    ("mine", "theirs", "expected"),
    [
        (GOLD, DIAMOND, "higher"),
        (GOLD, GOLD, "same"),
        (GOLD, SILVER, "lower"),
        (BRONZE, DIAMOND, "higher"),
        (DIAMOND, BRONZE, "lower"),
    ],
)
def test_relative_bracket(mine, theirs, expected):
    assert _relative(mine, theirs) == expected


def test_the_specified_rows_are_what_was_asked_for():
    """Same bracket is +10 / -10; beating somebody higher is +15, losing to
    them is -5."""
    assert BATTLE_POINTS["same"] == (10, -10)
    assert BATTLE_POINTS["higher"] == (15, -5)


def test_beating_somebody_below_you_is_worth_less_than_beating_a_peer():
    """The derived row.

    Without it the quickest way up the ladder is to farm weaker opponents,
    which is the one outcome a ranked ladder has to make unprofitable.
    """
    win_down, lose_down = BATTLE_POINTS["lower"]
    win_same, lose_same = BATTLE_POINTS["same"]

    assert win_down < win_same
    assert lose_down < lose_same


def test_the_table_is_symmetric_across_a_match():
    """What the winner gains and the loser pays are read from opposite rows.

    A match between a Gold and a Diamond pays the Gold +15 for a win and costs
    the Diamond -15 for the loss — the same encounter, read from each side.
    """
    gold_beats_diamond = BATTLE_POINTS[_relative(GOLD, DIAMOND)][0]
    diamond_loses_to_gold = BATTLE_POINTS[_relative(DIAMOND, GOLD)][1]

    assert gold_beats_diamond == 15
    assert diamond_loses_to_gold == -15


def test_every_bracket_pays_more_for_a_win_than_it_costs_to_lose_upward():
    """Playing up should always be worth trying.

    Somebody who only ever challenges above themselves risks 5 to make 15;
    somebody who only punches down risks 15 to make 5. That asymmetry is the
    whole point of the table.
    """
    assert BATTLE_POINTS["higher"][0] > abs(BATTLE_POINTS["higher"][1])
    assert BATTLE_POINTS["lower"][0] < abs(BATTLE_POINTS["lower"][1])


def test_no_row_is_worth_nothing():
    """A result that moved nobody would make a match pointless to play."""
    for win, loss in BATTLE_POINTS.values():
        assert win > 0
        assert loss < 0
