"""Journal routes, with the repository stubbed.

What is being tested here is the wiring — that the uid comes from the token
and that the server owns the computed figures — not Firestore.
"""

from __future__ import annotations

import logging
from decimal import Decimal

import pytest
from app.repositories import trades as repo
from app.schemas.trade import Trade
from google.api_core.exceptions import FailedPrecondition
from pydantic import ValidationError


def _stored(**overrides) -> Trade:
    defaults = dict(id="t1", ticker="NVDA", direction="Long", net_pl=Decimal("120"))
    return Trade(**{**defaults, **overrides})


def test_list_is_scoped_to_the_token_uid(client, monkeypatch, verified_user) -> None:
    seen: dict[str, str] = {}

    def fake_list(uid, *, limit, cursor):
        seen["uid"] = uid
        return [_stored()], None

    monkeypatch.setattr(repo, "list_trades", fake_list)

    response = client.get("/api/v1/trades")

    assert response.status_code == 200
    assert seen["uid"] == verified_user.uid
    assert response.json()["items"][0]["ticker"] == "NVDA"


def test_a_client_cannot_choose_whose_journal_to_read(
    client, monkeypatch, verified_user
) -> None:
    """A uid in the query string is ignored: there is no parameter for it."""
    seen: dict[str, str] = {}

    def fake_list(uid, *, limit, cursor):
        seen["uid"] = uid
        return [], None

    monkeypatch.setattr(repo, "list_trades", fake_list)

    client.get("/api/v1/trades?uid=someone-else")
    assert seen["uid"] == verified_user.uid


def test_created_trade_ignores_client_supplied_pl(client, monkeypatch) -> None:
    captured = {}

    def fake_create(uid, payload):
        captured["payload"] = payload
        return _stored()

    monkeypatch.setattr(repo, "create_trade", fake_create)

    response = client.post(
        "/api/v1/trades",
        json={"ticker": "NVDA", "direction": "Long", "net_pl": "99999"},
    )

    # Rejected outright rather than silently dropped, so a client that tries
    # learns that it cannot.
    assert response.status_code == 422


def test_delete_returns_no_content(client, monkeypatch) -> None:
    monkeypatch.setattr(repo, "delete_trade", lambda uid, trade_id: None)

    response = client.delete("/api/v1/trades/abc123")
    assert response.status_code == 204


def test_a_missing_index_is_not_reported_as_a_server_bug(
    client, monkeypatch, caplog
) -> None:
    """Firestore's FAILED_PRECONDITION means an index has not been deployed.

    It used to fall through to the generic 500, so a deployment step that was
    simply never run looked identical to a crash. The console URL Firestore
    supplies belongs in the log, where it is actionable, and nowhere near the
    response — it names collections and fields.
    """
    url = "https://console.firebase.google.com/v1/r/project/p/firestore/indexes?create_composite=Cg"

    def fake_list(uid, *, limit, cursor):
        raise FailedPrecondition(
            f"400 The query requires an index. Create it here: {url}"
        )

    monkeypatch.setattr(repo, "list_trades", fake_list)

    with caplog.at_level(logging.ERROR):
        response = client.get("/api/v1/trades")

    assert response.status_code == 503
    body = response.json()["error"]
    assert body["code"] == "database_not_ready"
    assert url not in response.text
    assert url in caplog.text


def test_a_screenshot_may_be_an_uploaded_key_or_a_link_and_nothing_else() -> None:
    """The field takes both shapes since the form offers both. It rejected the
    key shape once, which meant a trade with an uploaded chart could not save
    at all — and a ``javascript:`` URL still must not get through."""
    from app.schemas.trade import TradeCreate

    def screenshot_of(value: str) -> str:
        return TradeCreate(
            ticker="EURUSD", direction="Long", screenshot=value
        ).screenshot

    assert screenshot_of("charts/uid1/9f8e7d.png") == "charts/uid1/9f8e7d.png"
    assert screenshot_of("https://tradingview.com/x/abc") == "https://tradingview.com/x/abc"
    assert screenshot_of("") == ""

    rejected = ("javascript:alert(1)", "data:image/png;base64,AAA", "charts/../a.png")
    for value in rejected:
        with pytest.raises(ValidationError):
            screenshot_of(value)
