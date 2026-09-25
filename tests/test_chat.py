"""Contact search: what one trader may learn about another.

Conversations themselves are not tested here because they are not served here —
they live in the Realtime Database, and their boundary is database.rules.json.
What remains on this side is the directory, which is the one place a caller can
ask about somebody they have not met.
"""

from __future__ import annotations

from app.core.security import get_current_user
from app.models.repositories.directory import MIN_QUERY, search
from fastapi.testclient import TestClient


def test_short_searches_never_reach_the_database(monkeypatch) -> None:
    """A one- or two-character prefix would return an arbitrary slice of the
    user base. That is enumeration however small the page is."""

    def explode():  # pragma: no cover - must never be reached
        raise AssertionError("A too-short search reached Firestore.")

    monkeypatch.setattr("app.models.repositories.directory._collection", explode)

    for term in ("", "a", "ab", "  ab  "):
        assert search(term, exclude_uid="anyone") == []

    assert MIN_QUERY == 3


def test_the_directory_needs_a_session(app) -> None:
    with TestClient(app) as client:
        response = client.get("/api/v1/chat/directory", params={"q": "someone"})

    assert response.status_code == 401


def test_a_chat_token_needs_a_session(app) -> None:
    """The live connection is only as private as the thing that hands out
    credentials for it."""
    with TestClient(app) as client:
        response = client.get("/api/v1/chat/token")

    assert response.status_code == 401


def test_a_chat_token_is_only_ever_for_the_caller(
    app, verified_user, monkeypatch
) -> None:
    """No parameter, no body, nothing a client sends chooses the uid — it comes
    from the verified session and nowhere else."""
    minted: dict[str, str] = {}

    def fake_create_custom_token(uid: str) -> bytes:
        minted["uid"] = uid
        return b"token-for-" + uid.encode()

    monkeypatch.setattr(
        "app.controllers.v1.chat.firebase_auth.create_custom_token",
        fake_create_custom_token,
    )
    app.dependency_overrides[get_current_user] = lambda: verified_user

    with TestClient(app) as client:
        # A uid in the query string is ignored: there is no parameter for it.
        response = client.get("/api/v1/chat/token", params={"uid": "someone-else"})

    assert response.status_code == 200
    assert minted["uid"] == verified_user.uid
    assert response.json()["token"] == f"token-for-{verified_user.uid}"
