"""The browser's session: one opaque, HttpOnly cookie.

What the client holds is a Firebase *session cookie* — minted by the Admin SDK
from a freshly-issued ID token, verifiable only by this service, and readable by
no script on the page. The ID token it was minted from is discarded immediately;
it never reaches the browser.

This is the whole reason the front end can carry no Firebase configuration. A
bearer token in localStorage is a credential in the browser, readable by any
script that gets injected into the page. A cookie the page cannot read is not.

The cost is CSRF: a cookie is attached by the browser automatically, including
on a request some other site caused. ``SameSite`` is the defence, which is why
the API and the app are arranged to be same-site — see ``session_cookie_samesite``
in the settings, and the rewrite in the client's vercel.json.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from fastapi import Response, status
from firebase_admin import auth as firebase_auth

from app.core.config import Settings
from app.core.errors import AppError

logger = logging.getLogger(__name__)


class SessionError(AppError):
    def __init__(self, message: str = "Sign in to continue.") -> None:
        super().__init__(
            message, status_code=status.HTTP_401_UNAUTHORIZED, code="unauthorized"
        )


def mint(id_token: str, settings: Settings) -> str:
    """Exchange an ID token for a session cookie.

    Firebase refuses to mint from a token older than five minutes, which is
    exactly the property that makes this safe: the caller must have just proven
    who they are, not replayed something they kept.
    """
    try:
        cookie: str = firebase_auth.create_session_cookie(
            id_token, expires_in=timedelta(days=settings.session_days)
        )
        return cookie
    except Exception as exc:
        logger.warning("Session mint failed: %s", type(exc).__name__)
        raise SessionError("Could not start your session. Try signing in again.") from exc


def attach(response: Response, cookie: str, settings: Settings) -> None:
    response.set_cookie(
        key=settings.session_cookie_name,
        value=cookie,
        max_age=settings.session_days * 24 * 60 * 60,
        # The three that matter: unreadable by script, sent only over TLS, and
        # not attached to requests another site initiated.
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite=settings.session_cookie_samesite,
        domain=settings.session_cookie_domain,
        path="/",
    )


def clear(response: Response, settings: Settings) -> None:
    """Remove the cookie. The attributes must match the ones it was set with,
    or the browser keeps the original and sign-out silently does nothing."""
    response.delete_cookie(
        key=settings.session_cookie_name,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite=settings.session_cookie_samesite,
        domain=settings.session_cookie_domain,
        path="/",
    )


def verify(cookie: str) -> dict[str, object]:
    """Check a session cookie and return its claims.

    ``check_revoked`` costs a lookup but means a signed-out or disabled session
    stops working immediately rather than at expiry — which is what makes
    ``revoke`` below a real sign-out rather than a cosmetic one.
    """
    try:
        claims: dict[str, object] = firebase_auth.verify_session_cookie(
            cookie, check_revoked=True
        )
        return claims
    except firebase_auth.ExpiredSessionCookieError as exc:
        raise SessionError("Your session expired. Sign in again.") from exc
    except firebase_auth.RevokedSessionCookieError as exc:
        raise SessionError("Your session was ended. Sign in again.") from exc
    except firebase_auth.UserDisabledError as exc:
        raise SessionError("This account is disabled.") from exc
    except Exception as exc:
        # The reason is for us, not the caller: telling someone probing whether
        # a cookie was malformed or merely wrongly signed helps only them.
        logger.warning("Session verification failed: %s", type(exc).__name__)
        raise SessionError("Could not verify your session.") from exc


def revoke(uid: str) -> None:
    """End every session this account has, everywhere.

    Signing out on one device signs out on all of them. That is the stronger
    behaviour and the one worth having: a session someone wants gone is usually
    one they have lost control of.
    """
    try:
        firebase_auth.revoke_refresh_tokens(uid)
    except Exception as exc:
        logger.warning("Could not revoke sessions for %s: %s", uid, type(exc).__name__)
