"""A losing account is never described as a good one.

The coach used to read a healthy win rate off the top of the headline list and
congratulate a trader who was thousands down. The win rate was true; the
conclusion was nonsense. Win rate is how often they win, never how much they
keep, and the two part company the moment the losers run bigger than the
winners.
"""

from __future__ import annotations

from decimal import Decimal

from app.models.schemas.trade import Trade
from app.services.coach import build_system_prompt, headline_facts, standing
from app.services.stats import summarise


def _trades(*results: str) -> list[Trade]:
    return [
        Trade(id=f"t{index}", ticker="NVDA", direction="Long", net_pl=Decimal(value))
        for index, value in enumerate(results)
    ]


def _wins_often_loses_badly() -> list[Trade]:
    """Eight small winners and two big losers: an 80% win rate on an account
    that is three thousand down. The exact shape this whole file is about."""
    return _trades(*["250"] * 8, *["-2500"] * 2)


def test_a_down_account_is_named_as_down() -> None:
    summary = summarise(_wins_often_loses_badly())
    verdict = standing(summary)

    assert summary.net_pl < 0
    assert "DOWN" in verdict
    assert "3,000" in verdict
    assert "losing money" in verdict


def test_a_high_win_rate_on_a_losing_account_is_called_out() -> None:
    """The trap, named in the data rather than left for the model to spot."""
    verdict = standing(summarise(_wins_often_loses_badly()))

    assert "80% of their trades and still" in verdict
    assert "average loss is bigger than the average win" in verdict


def test_the_bottom_line_is_stated_before_the_win_rate() -> None:
    """Order is the point. A lone win rate near the top of a list reads as a
    verdict, and it is not one."""
    facts = headline_facts(summarise(_wins_often_loses_badly()))

    assert facts.index("NET P&L OVERALL") < facts.index("win rate")


def test_the_win_rate_never_appears_unqualified() -> None:
    facts = headline_facts(summarise(_wins_often_loses_badly()))

    assert "says nothing on its own about whether they are making money" in facts


def test_a_winning_account_is_not_flattered() -> None:
    verdict = standing(summarise(_trades("500", "400", "-100")))

    assert "up 800" in verdict
    assert "not the same as being good" in verdict


def test_nothing_closed_means_no_verdict_at_all() -> None:
    assert "no result to judge" in standing(summarise([]))


def test_the_standing_reaches_the_prompt() -> None:
    """Guards the wiring, not the wording: the verdict is computed and then has
    to actually be handed to the model."""
    summary = summarise(_wins_often_loses_badly())
    prompt = build_system_prompt(summary, "English", "Alex")

    assert standing(summary) in prompt
    assert "Win rate is how OFTEN they win" in prompt
