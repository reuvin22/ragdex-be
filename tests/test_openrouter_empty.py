"""What happens when the model sends back nothing usable.

"The coach returned an empty answer" was one sentence with no log line behind
it, and it covers three different faults that need three different fixes: a
truncated reply, a model that only thought out loud, and a genuinely empty
message. These pin the difference — and the leak that sat next to it.
"""

from __future__ import annotations

import logging

import httpx
import pytest
from app.core.config import Settings
from app.core.errors import AppError
from app.services import openrouter
from pydantic import SecretStr


def _settings(**overrides) -> Settings:
    return Settings(
        openrouter_api_key=SecretStr("sk-or-test"),
        openrouter_model="test/model",
        **overrides,
    )


def _reply(monkeypatch, payload: dict, status: int = 200) -> dict:
    """Answer the next OpenRouter call with this payload."""
    sent: dict = {}

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, *, json, headers):
            sent.update(json)
            return httpx.Response(status, json=payload)

    monkeypatch.setattr(openrouter.httpx, "AsyncClient", FakeClient)
    return sent


def _message(**message) -> dict:
    return {"choices": [{"message": message, "finish_reason": "stop"}]}


async def _ask(settings=None):
    return await openrouter.complete(
        messages=[{"role": "user", "content": "hi"}],
        settings=settings or _settings(),
    )


def test_a_truncated_reply_says_it_ran_out_of_room(monkeypatch, caplog) -> None:
    """Distinct from an empty one: this is a budget that was too small, not a
    model that said nothing. Retried first; this fake never relents, so it ends
    at the ceiling and reports the truncation."""
    _reply(
        monkeypatch,
        {"choices": [{"message": {"content": ""}, "finish_reason": "length"}]},
    )

    with caplog.at_level(logging.WARNING), pytest.raises(AppError) as caught:
        await_sync(_ask())

    assert "ran out of room" in caught.value.message
    assert "The budget ran out before the answer began." in caplog.text
    assert "Retrying the same request" in caplog.text


def test_reasoning_only_is_named_in_the_log(monkeypatch, caplog) -> None:
    """Reasoning models can put their thinking in its own field and leave
    content empty. It is counted, never shown."""
    _reply(
        monkeypatch,
        _message(content="", reasoning="Let me think about their win rate..."),
    )

    with caplog.at_level(logging.ERROR), pytest.raises(AppError):
        await_sync(_ask())

    assert "only its own reasoning" in caplog.text
    # The thinking itself never reaches a log line either.
    assert "win rate" not in caplog.text


def test_an_empty_message_is_reported_as_empty(monkeypatch, caplog) -> None:
    _reply(monkeypatch, _message(content=""))

    with caplog.at_level(logging.ERROR), pytest.raises(AppError) as caught:
        await_sync(_ask())

    assert caught.value.message == "The coach returned an empty answer."
    assert "empty message" in caplog.text


def test_a_truncated_thinking_block_is_not_shown_to_the_trader(monkeypatch) -> None:
    """The leak that sat next to the empty answer.

    The paired regex needs both tags, so a reply cut off mid-thought did not
    match it and the model's raw monologue went straight to the trader.
    """
    _reply(
        monkeypatch,
        _message(content="<think>They are down 3k, so I should"),
    )

    with pytest.raises(AppError) as caught:
        await_sync(_ask())

    assert "empty answer" in caught.value.message


def test_a_closed_thinking_block_leaves_the_answer_behind(monkeypatch) -> None:
    _reply(
        monkeypatch,
        _message(content="<think>hmm</think>You are down three thousand."),
    )

    completion = await_sync(_ask())

    assert completion.text == "You are down three thousand."


def test_the_budget_comes_from_settings(monkeypatch) -> None:
    sent = _reply(monkeypatch, _message(content="Fine."))

    await_sync(_ask(_settings(openrouter_max_tokens=4_000)))

    assert sent["max_tokens"] == 4_000


def await_sync(coroutine):
    """Run one coroutine. Simpler here than an async test plugin for six
    tests that never await anything real."""
    import asyncio

    return asyncio.run(coroutine)


def test_a_truncated_reply_is_retried_with_more_room(monkeypatch) -> None:
    """A model that thinks its way through the whole budget is a number that
    was too small, not a broken coach. A slower reply beats an error."""
    budgets: list[int] = []
    replies = [
        {"choices": [{"message": {"content": ""}, "finish_reason": "length"}]},
        _message(content="Take smaller size tomorrow."),
    ]

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, *, json, headers):
            budgets.append(json["max_tokens"])
            return httpx.Response(200, json=replies[len(budgets) - 1])

    monkeypatch.setattr(openrouter.httpx, "AsyncClient", FakeClient)

    completion = await_sync(_ask(_settings(openrouter_max_tokens=1_000)))

    assert budgets == [1_000, 2_000]
    assert completion.text == "Take smaller size tomorrow."


def test_the_retry_stops_at_the_ceiling(monkeypatch, caplog) -> None:
    """Past the ceiling the problem is the prompt, not the room."""
    calls: list[int] = []

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, *, json, headers):
            calls.append(json["max_tokens"])
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {"message": {"content": ""}, "finish_reason": "length"}
                    ]
                },
            )

    monkeypatch.setattr(openrouter.httpx, "AsyncClient", FakeClient)

    with pytest.raises(AppError):
        await_sync(_ask(_settings(openrouter_max_tokens=openrouter._RETRY_CEILING)))

    # Already at the ceiling, so it fails rather than looping.
    assert calls == [openrouter._RETRY_CEILING]
