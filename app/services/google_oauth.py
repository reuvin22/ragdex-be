"""Google sign-in, run from the server.

The OAuth handshake needs a browser — there is no way around that — but it does
not need the browser to *hold* anything. The client is sent to Google and comes
back here with a one-time code; this service exchanges it using the client
secret, and the page that started the flow never sees a token or a config.

The state parameter is the CSRF defence. It is signed with the service account
private key material rather than stored, so a horizontally-scaled deployment
needs no shared session store for a flow that lasts ten minutes.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import time
from urllib.parse import urlencode

import httpx
from fastapi import status

from app.core.config import Settings
from app.core.errors import AppError, UpstreamError

logger = logging.getLogger(__name__)

_AUTHORIZE = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN = "https://oauth2.googleapis.com/token"  # noqa: S105 - a URL, not a secret
_TIMEOUT = httpx.Timeout(20.0, connect=10.0)

# Long enough for a person to pick an account and type a password, short enough
# that a leaked callback URL is worthless by the time it is found.
_STATE_TTL_SECONDS = 600


class OAuthError(AppError):
    def __init__(self, message: str) -> None:
        super().__init__(
            message, status_code=status.HTTP_400_BAD_REQUEST, code="oauth_failed"
        )


def _secret(settings: Settings) -> bytes:
    """Key for signing state. Derived from the client secret, which is already
    the thing whose compromise would break this flow anyway."""
    if settings.google_client_secret is None:
        raise AppError(
            "Google sign-in is not configured on the server.",
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            code="not_configured",
        )
    return settings.google_client_secret.get_secret_value().encode()


def is_configured(settings: Settings) -> bool:
    return bool(settings.google_client_id) and settings.google_client_secret is not None


def _sign(payload: str, settings: Settings) -> str:
    digest = hmac.new(_secret(settings), payload.encode(), hashlib.sha256)
    return digest.hexdigest()


def make_state(settings: Settings) -> str:
    """A nonce and an expiry, signed. Nothing is stored server-side."""
    payload = f"{secrets.token_urlsafe(16)}.{int(time.time()) + _STATE_TTL_SECONDS}"
    return f"{payload}.{_sign(payload, settings)}"


def check_state(state: str, settings: Settings) -> None:
    parts = state.rsplit(".", 1)
    if len(parts) != 2:
        raise OAuthError("That sign-in link is not valid. Start again.")

    payload, signature = parts
    # compare_digest, not ==: a timing comparison on a signature is a real
    # forgery oracle given enough attempts.
    if not hmac.compare_digest(signature, _sign(payload, settings)):
        raise OAuthError("That sign-in link is not valid. Start again.")

    try:
        expires_at = int(payload.rsplit(".", 1)[1])
    except (IndexError, ValueError) as exc:
        raise OAuthError("That sign-in link is not valid. Start again.") from exc

    if time.time() > expires_at:
        raise OAuthError("That sign-in attempt timed out. Try again.")


def redirect_uri(settings: Settings) -> str:
    """Where Google sends the browser back to.

    Must match a redirect URI registered on the OAuth client exactly, including
    scheme and any trailing path.
    """
    base = settings.api_public_url.rstrip("/")
    return f"{base}{settings.api_v1_prefix}/auth/google/callback"


def authorize_url(state: str, settings: Settings) -> str:
    if not settings.google_client_id:
        raise AppError(
            "Google sign-in is not configured on the server.",
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            code="not_configured",
        )

    query = urlencode(
        {
            "client_id": settings.google_client_id,
            "redirect_uri": redirect_uri(settings),
            "response_type": "code",
            # openid+email is all Firebase needs to identify the account;
            # profile is what supplies the name and avatar.
            "scope": "openid email profile",
            "state": state,
            # Always ask which account, rather than silently reusing whichever
            # one the browser last used on some other Google property.
            "prompt": "select_account",
        }
    )
    return f"{_AUTHORIZE}?{query}"


async def exchange_code(code: str, settings: Settings) -> str:
    """Trade the one-time code for Google's ID token.

    Only the ID token is kept — it is handed straight to Identity Toolkit and
    then discarded. The access and refresh tokens are not requested for any
    purpose, so they are not retained.
    """
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.post(
                _TOKEN,
                data={
                    "code": code,
                    "client_id": settings.google_client_id or "",
                    "client_secret": _secret(settings).decode(),
                    "redirect_uri": redirect_uri(settings),
                    "grant_type": "authorization_code",
                },
            )
    except httpx.HTTPError as exc:
        logger.warning("Google token endpoint unreachable: %s", type(exc).__name__)
        raise UpstreamError("Google sign-in is unavailable right now.") from exc

    if not response.is_success:
        # The body names the client and the redirect URI; it goes to the log,
        # not to the browser.
        logger.warning("Google code exchange failed: %s", response.text[:400])
        raise OAuthError("Google sign-in did not complete. Try again.")

    id_token = str(response.json().get("id_token", ""))
    if not id_token:
        raise OAuthError("Google sign-in did not complete. Try again.")

    return id_token
