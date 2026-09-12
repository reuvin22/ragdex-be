"""The coach answers in the language the trader chose.

Worth its own file because it failed silently in a way that is easy to
reintroduce: the instruction was present and correctly worded, but sat between
the playbook and a multi-kilobyte stats dump, and smaller models skim the middle
of a long context. Position is the fix, so position is what these guard.
"""

from __future__ import annotations

from app.services.coach import ask, build_system_prompt, language_rule
from app.services.stats import summarise


def test_the_rule_names_the_language_and_forbids_english() -> None:
    rule = language_rule("Filipino")

    assert "write your entire reply in Filipino" in rule
    # "Reply in Filipino" leaves room to answer in English *about* Filipino.
    assert "Do not write in English" in rule


def test_english_is_not_told_to_avoid_itself() -> None:
    """A contradiction is the last thing to hand a model you are already
    struggling to instruct."""
    rule = language_rule("English")

    assert "write your entire reply in English" in rule
    assert "Do not write in English" not in rule


def test_the_rule_closes_the_system_prompt() -> None:
    """At the end, after the data. In the middle it was reliably ignored."""
    prompt = build_system_prompt(summarise([]), "Japanese", "Alex")

    assert prompt.rstrip().endswith(language_rule("Japanese"))


def test_the_rule_is_repeated_after_the_conversation(monkeypatch) -> None:
    """A long history pushes the system prompt far enough back that the rule at
    its end is lost too, so it is restated immediately before generation."""
    captured: dict[str, list[dict[str, str]]] = {}

    async def fake_complete(*, messages, settings, **kwargs):
        captured["messages"] = messages

        class Completion:
            text = "ok"
            model = "test"

        return Completion()

    monkeypatch.setattr("app.services.coach.openrouter.complete", fake_complete)

    import asyncio

    asyncio.run(
        ask(
            history=[],
            message="How am I doing?",
            summary=None,
            language="Spanish",
            display_name="Alex",
            settings=None,  # type: ignore[arg-type]
        )
    )

    messages = captured["messages"]
    assert messages[-1]["role"] == "system"
    assert "Spanish" in messages[-1]["content"]
    # And the trader's own question is still the last thing they said.
    assert messages[-2]["content"] == "How am I doing?"


def test_the_rule_refuses_an_in_conversation_switch() -> None:
    """It used to offer to switch when asked, which it could not honour.

    The language is pinned per request from the stored preference, so a switch
    agreed to in one reply was gone by the next turn — the trader asked for
    Korean and kept getting Tagalog. The rule now declines and names the
    control that does work.
    """
    rule = language_rule("Filipino")

    assert "do not switch" in rule.lower()
    assert "language button" in rule
    assert "unless they explicitly ask you to switch" not in rule
