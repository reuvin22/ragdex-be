"""A chart attached to a coach message.

The two things worth guarding are both about not lying to the trader. A chart
has to reach a model that can actually see one — a text model answers about the
words and ignores the picture, which reads as the coach having an opinion about
a chart it never looked at. And the image must be inline bytes, never a URL the
server would go and fetch.
"""

from __future__ import annotations

import pytest
from app.api.v1.routes import coach as route
from app.core.config import Settings
from app.schemas.coach import MAX_IMAGE, CoachRequest
from app.services import openrouter
from app.services.coach import ask
from pydantic import ValidationError

PNG = "data:image/png;base64,iVBORw0KGgo="


def _capture(monkeypatch) -> dict:
    captured: dict = {}

    async def fake_complete(*, messages, settings, **kwargs):
        captured["messages"] = messages
        captured["kwargs"] = kwargs

        class Completion:
            text = "Your stop was inside the range."
            model = "test"

        return Completion()

    monkeypatch.setattr(openrouter, "complete", fake_complete)
    return captured


def _run(coroutine):
    import asyncio

    return asyncio.run(coroutine)


def _ask(monkeypatch, *, image=None, message="what do you see?") -> dict:
    captured = _capture(monkeypatch)
    _run(
        ask(
            history=[],
            message=message,
            summary=None,
            language="English",
            display_name="Alex",
            settings=Settings(),
            image=image,
        )
    )
    return captured


def test_a_chart_goes_to_a_model_that_can_see_one(monkeypatch) -> None:
    captured = _ask(monkeypatch, image=PNG)

    assert captured["kwargs"]["models"] == list(openrouter.DEFAULT_VISION_MODELS)


def test_a_plain_question_stays_on_the_text_chain(monkeypatch) -> None:
    captured = _ask(monkeypatch)

    assert captured["kwargs"]["models"] is None


def test_the_image_travels_inline_in_the_user_turn(monkeypatch) -> None:
    captured = _ask(monkeypatch, image=PNG)

    content = captured["messages"][-2]["content"]
    assert content[0]["type"] == "text"
    assert content[1]["image_url"]["url"] == PNG


def test_a_chart_with_no_question_still_gets_one(monkeypatch) -> None:
    """A bare image otherwise gets described back at them like a caption."""
    captured = _ask(monkeypatch, image=PNG, message="")

    assert captured["messages"][-2]["content"][0]["text"].strip() != ""


def test_the_chart_rules_are_added_only_when_there_is_a_chart(monkeypatch) -> None:
    with_chart = _ask(monkeypatch, image=PNG)["messages"][0]["content"]
    without = _ask(monkeypatch)["messages"][0]["content"]

    assert "The trader has attached a chart" in with_chart
    assert "never say what it will do next" in with_chart
    assert "The trader has attached a chart" not in without


def test_only_inline_image_data_is_accepted() -> None:
    """A caller who could put a URL here would have the server fetch it. That
    is server-side request forgery, and inline bytes are the whole defence."""
    for rejected in (
        "https://example.com/chart.png",
        "data:text/html;base64,PHNjcmlwdD4=",
        "javascript:alert(1)",
        "data:image/svg+xml;base64,PHN2Zz4=",
    ):
        with pytest.raises(ValidationError):
            CoachRequest(message="hi", image=rejected)


def test_the_three_browser_formats_are_allowed() -> None:
    for accepted in ("png", "jpeg", "webp"):
        request = CoachRequest(message="hi", image=f"data:image/{accepted};base64,AA==")
        assert request.image is not None


def test_an_oversized_image_is_refused() -> None:
    with pytest.raises(ValidationError):
        CoachRequest(message="hi", image=PNG + "A" * MAX_IMAGE)


def test_a_message_or_a_chart_is_required() -> None:
    """A chart can carry the question on its own; nothing at all cannot."""
    assert CoachRequest(message="", image=PNG).image == PNG
    assert CoachRequest(message="hi").image is None

    with pytest.raises(ValidationError):
        CoachRequest(message="")


def test_the_chart_is_noted_but_not_stored(client, monkeypatch) -> None:
    """Forty turns of inline screenshots would pass a Firestore document's size
    ceiling, and the whole conversation would stop loading rather than one
    image going missing."""
    saved: dict = {}
    _capture(monkeypatch)
    monkeypatch.setattr(route.trades_repo, "all_trades", lambda uid: [])
    # The route reads the trader own capital and risk limits to grade
    # execution against them; none of that is under test here.
    monkeypatch.setattr(route.profiles_repo, "get_profile", lambda uid: None)
    monkeypatch.setattr(route.conversations_repo, "load_turns", lambda uid: [])
    monkeypatch.setattr(
        route.conversations_repo,
        "store_exchange",
        lambda uid, *, question, answer: saved.update(question=question),
    )

    response = client.post(
        "/api/v1/coach/chat", json={"message": "read this", "image": PNG}
    )

    assert response.status_code == 200
    assert saved["question"] == "read this [chart attached]"
    assert PNG not in saved["question"]
