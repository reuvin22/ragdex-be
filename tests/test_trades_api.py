"""Journal routes, with the repository stubbed.

What is being tested here is the wiring — that the uid comes from the token
and that the server owns the computed figures — not Firestore.
"""

from __future__ import annotations

from decimal import Decimal

from app.repositories import trades as repo
from app.schemas.trade import Trade


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


def test_a_client_cannot_choose_whose_journal_to_read(client, monkeypatch, verified_user) -> None:
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
