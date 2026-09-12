"""The coach diagnoses behaviour, not outcomes.

These guard the wiring rather than the wording: the playbook and the rules that
turn computed figures into behavioural evidence have to actually reach the
model. Prose can be tuned in coach.md without touching this file; a section
silently dropping out of the prompt is what it exists to catch.
"""

from __future__ import annotations

from decimal import Decimal

from app.schemas.trade import Trade
from app.services.coach import (
    BEHAVIOUR_EVIDENCE,
    build_system_prompt,
    playbook,
)
from app.services.stats import summarise


def _flat(text: str) -> str:
    """One long line. These constants are wrapped to fit the source file, so a
    phrase worth asserting on is usually split across two lines — and rewrapping
    a paragraph should not break a test about what it says."""
    return " ".join(text.split())


def _summary():
    trades = [
        Trade(id=f"t{index}", ticker="NVDA", direction="Long", net_pl=Decimal("250"))
        for index in range(8)
    ]
    return summarise(trades)


def test_the_behavioural_figures_are_labelled_as_behaviour() -> None:
    """Several of the computed numbers are not performance at all.

    ``avg_size_change_pct`` after a loss is a trader sizing up to win it back,
    already measured — but only if the prompt says so. Left unexplained it is
    one more line in a statistics dump.
    """
    prompt = _flat(build_system_prompt(_summary(), "English", "Alex"))

    assert _flat(BEHAVIOUR_EVIDENCE) in prompt
    assert "size UP after losing" in prompt
    assert "revenge trading with a price tag on it" in prompt


def test_compliance_is_named_as_the_thing_that_separates_the_two_failures() -> None:
    """A strategy problem and an execution problem look identical in the P&L
    and need opposite fixes. Plan compliance is what tells them apart."""
    prompt = _flat(build_system_prompt(_summary(), "English", "Alex"))

    assert "High compliance and poor results is a strategy problem" in prompt
    assert "Poor compliance is an execution problem" in prompt


def test_the_playbook_carries_the_diagnostic_method() -> None:
    voice = playbook()

    assert "## How you diagnose" in voice
    assert "## What you prescribe" in voice


def test_the_playbook_reaches_the_prompt_intact() -> None:
    prompt = _flat(build_system_prompt(_summary(), "English", "Alex"))

    assert _flat(playbook()) in prompt
