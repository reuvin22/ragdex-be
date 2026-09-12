"""Email confirmation: the gate every new account passes through.

The point of this flow is that a Google sign-in is held back like any other.
Google marks its accounts ``email_verified`` itself, so that flag cannot be the
gate — these tests are about the flag that can be.
"""

from __future__ import annotations

import json
import time

import pytest
from app.core import confirmation
from app.core.config import Settings
from app.core.errors import AppError
from fastapi.testclient import TestClient

# Shaped like the real thing; only the private key's bytes matter for signing.
_FAKE_ACCOUNT = json.dumps(
    {
        "type": "service_account",
        "project_id": "p",
        "client_email": "a@b.c",
        "private_key": "-----BEGIN PRIVATE KEY-----not-real-----END PRIVATE KEY-----",
    }
)


@pytest.fixture
def signing_settings() -> Settings:
    return Settings(firebase_service_account=_FAKE_ACCOUNT)


def test_a_token_round_trips(signing_settings) -> None:
    token = confirmation.issue("trader-1", signing_settings)
    assert confirmation.redeem(token, signing_settings) == "trader-1"


def test_a_tampered_uid_is_refused(signing_settings) -> None:
    """The whole point: a link cannot be edited to confirm somebody else."""
    token = confirmation.issue("trader-1", signing_settings)
    forged = token.replace("trader-1", "trader-2", 1)

    with pytest.raises(AppError):
        confirmation.redeem(forged, signing_settings)


def test_a_tampered_signature_is_refused(signing_settings) -> None:
    payload, _, signature = confirmation.issue("trader-1", signing_settings).rpartition(
        "."
    )

    with pytest.raises(AppError):
        confirmation.redeem(f"{payload}.{signature[:-2]}xx", signing_settings)


def test_an_expired_token_is_refused(signing_settings, monkeypatch) -> None:
    token = confirmation.issue("trader-1", signing_settings)

    # A day and a minute later. The real clock is read first — a lambda that
    # called time.time() would be calling the patch, not the clock.
    later = time.time() + confirmation.TTL_SECONDS + 60
    monkeypatch.setattr(time, "time", lambda: later)

    with pytest.raises(AppError) as raised:
        confirmation.redeem(token, signing_settings)

    assert "expired" in raised.value.message.lower()


def test_nonsense_is_refused(signing_settings) -> None:
    for token in ("", "nope", "a.b", "....", "trader-1.9999999999"):
        with pytest.raises(AppError):
            confirmation.redeem(token, signing_settings)


def test_a_bad_link_redirects_rather_than_erroring(app) -> None:
    """Reached by clicking a link in an email, so there is nobody there to read
    JSON — every outcome has to be somewhere a browser can land."""
    with TestClient(app, follow_redirects=False) as client:
        response = client.get("/api/v1/auth/confirm", params={"token": "forged"})

    assert response.status_code == 303
    assert "confirm=invalid" in response.headers["location"]


def test_confirming_needs_no_session(app, monkeypatch) -> None:
    """The link often opens in a different browser from the one that asked for
    it. Requiring a cookie would fail those clicks for no gain — the signature
    is what authorises this, and it names the uid itself."""
    marked: list[str] = []

    monkeypatch.setattr(
        "app.api.v1.routes.auth.profile_repo.mark_confirmed", marked.append
    )
    monkeypatch.setattr(
        "app.api.v1.routes.auth.firebase_auth.update_user",
        lambda uid, **kwargs: None,
    )

    # The route resolves its own settings, so pin the signing key for both
    # sides rather than trying to hand it one.
    monkeypatch.setattr("app.core.confirmation._key", lambda _: b"test-key")
    token = confirmation.issue("trader-9", Settings())

    with TestClient(app, follow_redirects=False) as client:
        response = client.get("/api/v1/auth/confirm", params={"token": token})

    assert response.status_code == 303
    assert "confirm=ok" in response.headers["location"]
    assert marked == ["trader-9"]
