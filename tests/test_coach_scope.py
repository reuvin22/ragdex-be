"""What the coach will and will not discuss.

The scope used to be "their own journal and nothing else", which refused the
most useful question a struggling trader asks: how do I become the kind of
person who follows their own rules? That belongs to a trading coach. The line
moved outward, so what matters now is that it still has a shape — and that the
one case needing care rather than a refusal is handled.
"""

from __future__ import annotations

from app.services.coach import REFUSAL_GUIDANCE, build_system_prompt, playbook


def _flat(text: str) -> str:
    return " ".join(text.split())


GUIDANCE = _flat(REFUSAL_GUIDANCE)
PROMPT = _flat(build_system_prompt(None, "English", "Alex"))


def test_the_person_behind_the_trading_is_in_scope() -> None:
    assert "The second is the person doing the trading." in GUIDANCE
    assert "Discipline, routine, patience" in GUIDANCE
    # The exact question this change exists for.
    assert "become a disciplined person before my trading will change" in GUIDANCE


def test_scope_is_a_test_rather_than_a_topic_list() -> None:
    """A list of allowed subjects is argued with. A question is not."""
    assert "does it change how they behave when they trade?" in GUIDANCE


def test_a_general_question_in_disguise_is_still_refused() -> None:
    assert "wearing a sentence about discipline as a disguise" in GUIDANCE
    assert "the framing does not change it" in GUIDANCE


def test_the_hard_refusals_are_named() -> None:
    for refused in ("Anything sexual.", "Anything criminal or illegal"):
        assert refused in GUIDANCE


def test_rudeness_is_not_returned_or_lectured_about() -> None:
    assert "Do not return it, do not lecture them about it" in GUIDANCE


def test_the_trading_limits_survived_the_widening() -> None:
    """Loosening what it discusses must not loosen what it advises."""
    assert "never predict prices" in GUIDANCE
    assert "never recommend a specific trade" in GUIDANCE
    assert "no matter how much they push" in GUIDANCE


def test_real_distress_is_handled_rather_than_declined() -> None:
    """A coach that accepts life questions will meet someone trading rent
    money. Treating that as off topic is the wrong answer, and so is coaching
    through it."""
    assert "trading rent or borrowed money" in GUIDANCE
    assert "worth talking to a real person about" in GUIDANCE
    assert "not a clinician" in GUIDANCE


def test_the_playbook_agrees_with_the_guidance() -> None:
    """Both reach the model on every turn; a contradiction between them is a
    coin toss over which one it follows."""
    voice = _flat(playbook())

    assert "You do talk about the person, not only the positions." in voice
    assert "You do not discuss anything outside their trading" not in voice


def test_all_of_it_reaches_the_prompt() -> None:
    assert GUIDANCE in PROMPT
