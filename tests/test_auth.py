"""Sign-in: what is refused, and what is never revealed.

The disclosure tests matter as much as the rejection ones. These routes are the
only ones reachable without a session, so anything they say back is said to
anybody who asks.
"""

from __future__ import annotations

import pytest
from app.core.errors import AppError
from app.core.security import get_current_user
from app.schemas.auth import Credentials, Registration
from app.services.identity import CredentialsError, _translate
from fastapi.testclient import TestClient
from pydantic import ValidationError


def test_session_answers_with_no_user_when_signed_out(app) -> None:
    """Not signed in is an ordinary answer here, not an error.

    The client calls this on every load to choose between the app, the login
    screen and the verify gate; a 401 would make a first visit look broken.
    """
    with TestClient(app) as client:
        response = client.get("/api/v1/auth/session")

    assert response.status_code == 200
    assert response.json() == {"user": None}


def test_logout_requires_a_session(app) -> None:
    with TestClient(app) as client:
        response = client.post("/api/v1/auth/logout")

    assert response.status_code == 401


def test_a_wrong_password_and_an_unknown_account_look_identical() -> None:
    """Otherwise this is an oracle for which addresses are registered."""
    missing = _translate("EMAIL_NOT_FOUND")
    wrong = _translate("INVALID_PASSWORD")

    assert isinstance(missing, CredentialsError)
    assert isinstance(wrong, CredentialsError)
    assert missing.message == wrong.message
    assert missing.status_code == wrong.status_code == 401


def test_an_unmapped_upstream_code_is_not_echoed() -> None:
    """An unrecognised code is one we have not decided is safe to reveal, so it
    becomes a generic failure rather than passing through."""
    error = _translate("SOME_NEW_INTERNAL_REASON")

    assert isinstance(error, AppError)
    assert "SOME_NEW_INTERNAL_REASON" not in error.message


def test_password_reset_does_not_confirm_whether_an_account_exists(
    app, monkeypatch
) -> None:
    async def refuse(email, settings):
        raise CredentialsError()

    monkeypatch.setattr("app.services.identity.send_password_reset", refuse)

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/auth/password-reset", json={"email": "nobody@example.com"}
        )

    assert response.status_code == 200
    assert "if that address" in response.json()["message"].lower()


def test_a_short_password_is_refused_before_it_leaves() -> None:
    """Matching Firebase's own floor means the rejection is ours, with a
    readable message, rather than a WEAK_PASSWORD after a round trip."""
    with pytest.raises(ValidationError):
        Credentials(email="trader@example.com", password="12345")


def test_credentials_carry_nothing_extra() -> None:
    with pytest.raises(ValidationError):
        Registration(
            email="trader@example.com",
            password="good-password",
            uid="someone-elses-uid",  # type: ignore[call-arg]
        )


def test_a_verified_session_is_still_required_to_send_verification(app) -> None:
    """The address comes from the session, never a body — otherwise this is a
    way to aim mail at someone else's inbox from our domain."""
    with TestClient(app) as client:
        response = client.post("/api/v1/auth/verify-email")

    assert response.status_code == 401


def test_the_session_dependency_is_what_routes_depend_on(app, verified_user) -> None:
    """A sanity check on the wiring: overriding identity reaches the routes, so
    the rest of the suite's overrides mean what they claim."""
    app.dependency_overrides[get_current_user] = lambda: verified_user

    with TestClient(app) as client:
        response = client.post("/api/v1/auth/logout")

    assert response.status_code != 401
