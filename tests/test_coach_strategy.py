"""Wanting to change strategy is a coaching question, not a refusal.

The coach turned it down three times in a row: "I can't design a new Breakout,
liquidity sweep and FVG system for you." It had over-read the rule against
recommending trades into a rule against teaching anything, which refuses the
most ordinary question a struggling trader asks.
"""

from __future__ import annotations

from app.services.coach import (
    REFUSAL_GUIDANCE,
    STRATEGY_CHANGE,
    build_system_prompt,
)


def _flat(text: str) -> str:
    return " ".join(text.split())


GUIDANCE = _flat(REFUSAL_GUIDANCE)
STRATEGY = _flat(STRATEGY_CHANGE)


def test_teaching_a_setup_is_separated_from_calling_a_trade() -> None:
    """The distinction the model collapsed. Both halves have to be stated or it
    collapses them again."""
    assert "It forbids calling a trade. It does NOT forbid teaching." in GUIDANCE
    assert '"I can\'t design a strategy for you" is the wrong answer' in GUIDANCE
    assert "refusing it is a failure, not caution" in GUIDANCE


def test_calling_a_trade_is_still_refused() -> None:
    """Widening what it teaches must not widen what it advises."""
    assert "never recommend a specific trade" in GUIDANCE
    assert '"buy this breakout" is not yours to say' in GUIDANCE


def test_a_setup_absent_from_the_journal_is_allowed() -> None:
    """The trader's actual objection: "even not in my journal since I want to
    change my strategy"."""
    assert "a setup that is not in their journal yet" in GUIDANCE
    assert (
        "everyone's first trade of a new setup is one they have never logged"
        in GUIDANCE
    )


def test_the_coach_neither_refuses_nor_simply_agrees() -> None:
    assert "Do not refuse, and do not simply agree either" in STRATEGY


def test_adherence_decides_which_problem_it_is() -> None:
    """High adherence with poor results is a real strategy problem. Low
    adherence means the strategy was never tested, and switching resets the
    sample while the habit comes along unchanged."""
    assert "high adherence with poor results genuinely is a strategy problem" in STRATEGY
    assert "switching now means they will not know whether the next one works" in STRATEGY


def test_the_fork_is_put_to_the_trader() -> None:
    """Their money, their account — the coach advises and then asks."""
    assert "let them decide" in STRATEGY
    assert (
        "CHOICE: Yes, teach me the new strategy | No, help me fix the one I have"
        in STRATEGY
    )


def test_both_branches_are_specified() -> None:
    assert "If they choose to learn the new one: teach it properly." in STRATEGY
    assert "If they choose to fix what they have" in STRATEGY
    # The three the trader asked for, by name.
    assert "The psychology" in STRATEGY
    assert "The execution" in STRATEGY
    assert "The data" in STRATEGY


def test_the_choice_marker_is_explained_where_formatting_lives() -> None:
    """The no-markdown rule would otherwise have the model suppress it."""
    prompt = _flat(build_system_prompt(None, "English", "Alex"))

    assert '"CHOICE: first option | second option" becomes buttons' in prompt
    assert "never write more than one" in prompt


def test_it_all_reaches_the_prompt() -> None:
    prompt = _flat(build_system_prompt(None, "English", "Alex"))

    assert STRATEGY in prompt
    assert GUIDANCE in prompt
