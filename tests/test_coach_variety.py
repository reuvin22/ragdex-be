"""Different questions get different answers.

Two consecutive replies once ran the identical four-paragraph skeleton — the
account total, the rule, what it costs, then what not to do — because the prompt
laid that out as three numbered parts "in this order" and the model filled the
form in. A follow-up asking how to apply the rule got the rule restated.

These guard the two things that fixed it: nothing in the prompt prescribes a
running order any more, and the model is shown its own last opening immediately
before it writes.
"""

from __future__ import annotations

from decimal import Decimal

from app.schemas.coach import CoachTurn
from app.schemas.trade import Trade
from app.services import openrouter
from app.services.coach import _no_repeats, ask, build_system_prompt
from app.services.stats import summarise


def _flat(text: str) -> str:
    return " ".join(text.split())


def _summary():
    trades = [
        Trade(id=f"t{index}", ticker="NVDA", direction="Long", net_pl=Decimal("-180"))
        for index in range(20)
    ]
    return summarise(trades)


def _capture(monkeypatch) -> dict:
    captured: dict = {}

    async def fake_complete(*, messages, settings, **kwargs):
        captured["messages"] = messages
        captured["kwargs"] = kwargs

        class Completion:
            text = "ok"
            model = "test"

        return Completion()

    monkeypatch.setattr(openrouter, "complete", fake_complete)
    return captured


def _run(coroutine):
    import asyncio

    return asyncio.run(coroutine)


def _ask(monkeypatch, history: list[CoachTurn]) -> dict:
    captured = _capture(monkeypatch)
    _run(
        ask(
            history=history,
            message="how can I do this?",
            summary=_summary(),
            language="Filipino",
            display_name="Alex",
            settings=None,
        )
    )
    return captured


def test_the_last_opening_is_quoted_back_before_generating() -> None:
    """An abstract "do not repeat yourself" is easy to agree with and ignore.
    Its own words are harder to write past."""
    reminder = _no_repeats(
        [
            CoachTurn(role="user", text="what should I do?"),
            CoachTurn(
                role="coach",
                text="You are down about $3,744 across 20 trades.\n\nSet one rule.",
            ),
        ]
    )

    assert "You are down about $3,744 across 20 trades." in reminder
    assert "must not open the same way" in reminder
    # Only the opening line, not the whole previous reply pasted back in.
    assert "Set one rule" not in reminder


def test_a_first_message_gets_no_repetition_reminder() -> None:
    assert _no_repeats([]) == ""
    assert _no_repeats([CoachTurn(role="user", text="hello")]) == ""


def test_the_reminder_rides_with_the_language_rule(monkeypatch) -> None:
    """Both belong in the same trailing system message: it is the last thing
    read before generation, and the repetition rule needs the previous replies
    still in view."""
    captured = _ask(
        monkeypatch,
        [
            CoachTurn(role="user", text="what should I do?"),
            CoachTurn(role="coach", text="You are down about $3,744."),
        ],
    )

    closing = captured["messages"][-1]
    assert closing["role"] == "system"
    assert "DO NOT REPEAT YOURSELF" in closing["content"]
    assert "You are down about $3,744." in closing["content"]
    assert "write your entire reply in Filipino" in closing["content"]


def test_the_prompt_no_longer_prescribes_a_running_order() -> None:
    """The exact wording that produced the four-paragraph form."""
    prompt = _flat(build_system_prompt(_summary(), "Filipino", "Alex"))

    assert "Three parts, in this order" not in prompt
    assert "NOT a running order and NOT a template" in prompt
    assert "you have stopped coaching and started filling in a form" in prompt


def test_a_follow_up_is_told_to_answer_the_follow_up() -> None:
    prompt = _flat(build_system_prompt(_summary(), "Filipino", "Alex"))

    assert "They have heard the why. Give them the how." in prompt


def test_the_standing_is_not_an_opening_line() -> None:
    """It opened every single reply, because it led the data block."""
    prompt = _flat(build_system_prompt(_summary(), "Filipino", "Alex"))

    assert "Neither is a line to open every reply with" in prompt


def test_the_language_rule_asks_for_a_speaker_not_a_translator(monkeypatch) -> None:
    prompt = _flat(build_system_prompt(_summary(), "Filipino", "Alex"))

    assert "as a native speaker talking, not as someone translating" in prompt
    assert "Do not carry English sentence structure" in prompt


def test_the_coach_runs_warmer_than_the_default(monkeypatch) -> None:
    """A fixed data set pulls hard towards the same paragraph every time."""
    captured = _ask(monkeypatch, [])

    assert captured["kwargs"]["temperature"] == 0.9
