"""Journal routes, with the repository stubbed.

What is being tested here is the wiring — that the uid comes from the token
and that the server owns the computed figures — not Firestore.
"""

from __future__ import annotations

import logging
from decimal import Decimal

import pytest
from app.models.repositories import trades as repo
from app.models.schemas.trade import Trade
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


def test_created_trade_accepts_the_traders_own_pl(client, monkeypatch) -> None:
    captured = {}

    def fake_create(uid, payload):
        captured["payload"] = payload
        return _stored()

    monkeypatch.setattr(repo, "create_trade", fake_create)

    response = client.post(
        "/api/v1/trades",
        json={"ticker": "ES", "direction": "Long", "net_pl": "-312.50"},
    )

    assert response.status_code == 201
    assert captured["payload"].net_pl == Decimal("-312.50")


def test_server_owned_fields_are_still_refused(client) -> None:
    response = client.post(
        "/api/v1/trades",
        json={"ticker": "NVDA", "direction": "Long", "uid": "someone-else"},
    )
    assert response.status_code == 422


def test_entered_risk_reward_wins_over_stop_and_target() -> None:
    from app.models.repositories.trades import _derive

    base = {"entryPrice": 100.0, "stopLoss": 99.0, "takeProfit": 103.0}

    assert _derive({**base, "riskReward": 2.5})["riskReward"] == 2.5
    assert _derive({**base, "riskReward": None})["riskReward"] == 3.0
    assert _derive({"entryPrice": 100.0})["riskReward"] is None
    # No stop or target on file, but the trader planned one.
    assert _derive({"entryPrice": 100.0, "riskReward": 2.0})["riskReward"] == 2.0


def test_a_ratio_past_100_is_refused() -> None:
    from app.models.schemas.trade import TradeCreate

    with pytest.raises(ValidationError):
        TradeCreate(ticker="NVDA", direction="Long", risk_reward=Decimal("250"))


def test_entered_pl_wins_over_the_price_arithmetic() -> None:
    """A futures contract moving 2 points is $100, not $2 — the trader's
    figure is the one the calendar has to show."""
    from app.models.repositories.trades import _derive

    base = {"entryPrice": 5000.0, "exitPrice": 5002.0, "size": 1.0, "direction": "Long"}

    assert _derive({**base, "netPl": 100.0})["netPl"] == 100.0
    assert _derive({**base, "netPl": None})["netPl"] == 2.0
    assert _derive({**base, "exitPrice": None})["netPl"] is None
    # An open trade the trader has already booked a partial result on.
    assert _derive({**base, "exitPrice": None, "netPl": 40.0})["netPl"] == 40.0


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


def test_screenshots_may_be_uploaded_keys_or_links_and_nothing_else() -> None:
    """The field takes both shapes since the form offers both. It rejected the
    key shape once, which meant a trade with an uploaded chart could not save
    at all — and a ``javascript:`` URL still must not get through."""
    from app.models.schemas.trade import TradeCreate

    def kept(*values: str) -> list[str]:
        return TradeCreate(
            ticker="EURUSD", direction="Long", screenshots=list(values)
        ).screenshots

    assert kept("charts/uid1/9f8e7d.png") == ["charts/uid1/9f8e7d.png"]
    assert kept("https://tradingview.com/x/a") == ["https://tradingview.com/x/a"]
    assert kept() == []

    # Several charts on one trade, with blanks and a repeat dropped.
    assert kept(
        "charts/uid1/a.png", "", "charts/uid1/b.png", "charts/uid1/a.png"
    ) == ["charts/uid1/a.png", "charts/uid1/b.png"]

    rejected = ("javascript:alert(1)", "data:image/png;base64,AAA", "charts/../a.png")
    for value in rejected:
        with pytest.raises(ValidationError):
            kept(value)


def test_a_trade_filed_before_charts_were_plural_still_opens() -> None:
    """One sealed string under the old key, lifted into the list — the same
    treatment the session field got when it became plural."""
    from app.models.repositories.trades import _screenshots_of

    assert _screenshots_of({"screenshot": "charts/uid1/a.png"}) == ["charts/uid1/a.png"]
    assert _screenshots_of({"screenshot": ""}) == []
    assert _screenshots_of({"screenshots": ["a", None, 3, "b"]}) == ["a", "b"]
    assert _screenshots_of({}) == []


def test_a_trade_is_filed_under_its_journal_account() -> None:
    from app.models.repositories.trades import _SEALED, _to_document
    from app.models.schemas.trade import TradeCreate, TradeUpdate

    created = TradeCreate(ticker="US30", direction="Short", account=" FTMO Phase 2 ")
    assert _to_document(created, partial=False)["account"] == "FTMO Phase 2"
    # Moving a trade to another journal is a one-field patch.
    moved = TradeUpdate(account="FTMO Funded")
    assert _to_document(moved, partial=True) == {"account": "FTMO Funded"}
    assert "account" in _SEALED
    # Trades logged before journals existed belong to the default one.
    assert TradeCreate(ticker="US30", direction="Short").account == ""


def test_renaming_a_journal_is_scoped_to_the_token_uid(
    client, monkeypatch, verified_user
) -> None:
    seen: dict[str, str] = {}

    def fake_rename(uid, old, new):
        seen.update(uid=uid, old=old, new=new)
        return 7

    monkeypatch.setattr(repo, "rename_account", fake_rename)

    response = client.post(
        "/api/v1/trades/accounts/rename", json={"from_name": "", "to_name": "Phase 1"}
    )

    assert response.status_code == 200
    assert response.json() == {"moved": 7}
    assert seen == {"uid": verified_user.uid, "old": "", "new": "Phase 1"}
