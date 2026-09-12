"""What the coach is asked, and what it is allowed to answer with.

Two things are guarded here. The figures now carry the best of each cut as well
as the worst — "where does my edge live" is unanswerable from the worst setup
alone. And the prompt states the cuts that exist, because a coach told to break
performance down will otherwise break it down by weekday or instrument, neither
of which is computed, and produce a confident number from nothing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from app.schemas.trade import Trade
from app.services.coach import build_system_prompt, headline_facts
from app.services.stats import summarise


def _trade(index: int, net_pl: str, setup: str, hour: int) -> Trade:
    return Trade(
        id=f"t{index}",
        ticker="NVDA",
        direction="Long",
        net_pl=Decimal(net_pl),
        setup=setup,
        entry_at=datetime(2026, 9, 1, hour, 0, tzinfo=UTC),
    )


def _journal() -> list[Trade]:
    """A losing account with a profitable corner — the shape the edge-finding
    is for."""
    winners = [_trade(i, "300", "Opening Range Break", 9) for i in range(5)]
    losers = [_trade(i, "-700", "Afternoon Reversal", 14) for i in range(5, 10)]
    return winners + losers


def test_the_best_setup_is_handed_over_not_just_the_worst() -> None:
    facts = headline_facts(summarise(_journal()))

    assert "Worst setup by money: Afternoon Reversal" in facts
    assert "Best setup by money: Opening Range Break" in facts


def test_the_best_hour_is_handed_over_too() -> None:
    facts = headline_facts(summarise(_journal()))

    assert "Worst hour of the day: 14:00" in facts
    assert "Best hour of the day: 09:00" in facts


def test_a_bucket_carries_its_win_rate() -> None:
    """Money alone does not say whether a setup is working or just sized big."""
    facts = headline_facts(summarise(_journal()))

    assert "wins 100% of the time" in facts
    assert "wins 0% of the time" in facts


def test_no_best_is_claimed_when_nothing_makes_money() -> None:
    """"Your best hour loses the least" is not an edge and must not be dressed
    up as one."""
    losing = [_trade(i, "-100", "Fade", 11) for i in range(3)]
    losing += [_trade(i, "-400", "Chase", 15) for i in range(3, 6)]

    facts = headline_facts(summarise(losing))

    assert "Worst setup by money" in facts
    assert "Best setup by money" not in facts


def test_a_single_bucket_is_not_reported_as_both_ends() -> None:
    facts = headline_facts(summarise([_trade(0, "500", "Only One", 9)]))

    assert "Worst setup by money: Only One" in facts
    assert "Best setup by money" not in facts


def test_the_five_questions_reach_the_prompt() -> None:
    prompt = " ".join(build_system_prompt(summarise(_journal()), "English", "A").split())

    assert "Am I following my own rules?" in prompt
    assert "Is my strategy actually working?" in prompt
    assert "never answer this overall" in prompt


def test_the_cuts_that_do_not_exist_are_named() -> None:
    """Without this the model breaks performance down by weekday or instrument
    — neither of which is computed — and states a number it invented."""
    prompt = " ".join(build_system_prompt(summarise(_journal()), "English", "A").split())

    assert "You do NOT have performance by weekday, by instrument" in prompt
    assert "Never estimate it" in prompt


def test_the_coach_is_told_to_reuse_what_it_was_told() -> None:
    """The conversation persists between visits now, so asking someone to
    re-explain their own risk limit is how they learn you were not listening."""
    prompt = " ".join(build_system_prompt(summarise(_journal()), "English", "A").split())

    assert "Remember what they have told you." in prompt
