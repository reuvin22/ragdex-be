"""The rules that stop one trader reading another's journal.

These are the tests to keep even if everything else is deleted: a regression
here is a data breach, not a bug.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.core.errors import AppError
from app.core.security import RateLimiter, get_current_user, get_verified_user
from app.schemas.trade import TradeCreate


def test_unauthenticated_request_is_rejected(app) -> None:
    """No override for identity here: the real dependency runs and finds no
    bearer token."""
    with TestClient(app) as client:
        response = client.get("/api/v1/trades")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_write_requires_a_confirmed_email(app, unverified_user) -> None:
    """Mirrors firestore.rules, where an unverified session cannot write."""
    app.dependency_overrides[get_current_user] = lambda: unverified_user
    # get_verified_user is left real, so it applies its own check.

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/trades", json={"ticker": "NVDA", "direction": "Long"}
        )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "email_unverified"


def test_read_does_not_require_a_confirmed_email(app, unverified_user, monkeypatch) -> None:
    from app.repositories import trades as repo

    monkeypatch.setattr(repo, "list_trades", lambda uid, *, limit, cursor: ([], None))
    app.dependency_overrides[get_current_user] = lambda: unverified_user

    with TestClient(app) as client:
        response = client.get("/api/v1/trades")

    assert response.status_code == 200


def test_trade_payload_rejects_unknown_fields() -> None:
    """extra="forbid" is what stops a caller writing a field the API does not
    know about into a Firestore document."""
    with pytest.raises(ValueError):
        TradeCreate(ticker="NVDA", direction="Long", netPl=999_999)


def test_trade_payload_rejects_a_javascript_url() -> None:
    with pytest.raises(ValueError):
        TradeCreate(ticker="NVDA", direction="Long", screenshot="javascript:alert(1)")


def test_trade_payload_rejects_exit_before_entry() -> None:
    now = datetime.now(timezone.utc)
    with pytest.raises(ValueError):
        TradeCreate(
            ticker="NVDA",
            direction="Long",
            entry_at=now,
            exit_at=now - timedelta(hours=1),
        )


def test_rate_limiter_allows_then_blocks() -> None:
    limiter = RateLimiter(2, name="test")

    limiter.check("uid")
    limiter.check("uid")

    with pytest.raises(AppError) as caught:
        limiter.check("uid")

    assert caught.value.status_code == 429


def test_rate_limiter_budgets_are_per_user() -> None:
    """One noisy client must not be able to spend another's allowance."""
    limiter = RateLimiter(1, name="test")

    limiter.check("trader-a")
    limiter.check("trader-b")  # must not raise


def test_security_headers_are_present(client) -> None:
    response = client.get("/health")

    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    # Per-user responses must never be reused by a shared cache.
    assert response.headers["Cache-Control"] == "no-store"
    assert "X-Request-ID" in response.headers


def test_oversized_body_is_refused_before_the_route() -> None:
    """Exercised directly rather than through the app: the limit is read when
    the middleware is constructed, so a fixture that mutates settings after
    the app exists would prove nothing."""
    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route

    from app.core.middleware import BodySizeLimitMiddleware

    async def echo(request):  # pragma: no cover - must never be reached
        return PlainTextResponse("reached the route")

    tiny = Starlette(routes=[Route("/echo", echo, methods=["POST"])])
    tiny.add_middleware(BodySizeLimitMiddleware, max_bytes=100)

    with TestClient(tiny) as client:
        too_big = client.post("/echo", content=b"x" * 500)
        small_enough = client.post("/echo", content=b"x" * 10)

    assert too_big.status_code == 413
    assert too_big.json()["error"]["code"] == "payload_too_large"
    assert small_enough.status_code == 200


def test_error_envelope_never_leaks_internals(app, monkeypatch) -> None:
    from app.repositories import trades as repo

    def explode(uid, *, limit, cursor):
        raise RuntimeError("connection string postgres://user:password@host/db")

    monkeypatch.setattr(repo, "list_trades", explode)

    from app.core.security import CurrentUser

    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        uid="t", email=None, email_verified=True, name=None
    )
    app.dependency_overrides[get_verified_user] = lambda: CurrentUser(
        uid="t", email=None, email_verified=True, name=None
    )

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/api/v1/trades")

    assert response.status_code == 500
    body = response.text
    assert "password" not in body
    assert "RuntimeError" not in body
    assert response.json()["error"]["code"] == "internal_error"
