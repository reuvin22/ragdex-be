"""Firebase Identity Toolkit, reached from the server.

The Admin SDK can mint and verify tokens but cannot check a password, so the
password flows go through Identity Toolkit's REST API — the same endpoints the
browser SDK calls, with the difference that the Web API key stays here instead
of shipping in a bundle.

Nothing this module returns ever reaches a client verbatim. Identity Toolkit
answers with machine codes (``EMAIL_NOT_FOUND``, ``INVALID_LOGIN_CREDENTIALS``)
that are useful to us and, in a couple of cases, a disclosure risk: whether an
address has an account is not something an unauthenticated caller should be
able to enumerate. ``_translate`` is where that judgement lives.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx
from fastapi import status

from app.core.config import Settings
from app.core.errors import AppError, UpstreamError

logger = logging.getLogger(__name__)

_BASE = "https://identitytoolkit.googleapis.com/v1/accounts"
_TIMEOUT = httpx.Timeout(20.0, connect=10.0)


@dataclass(slots=True)
class IdentityResult:
    """What Identity Toolkit hands back after a successful sign-in."""

    uid: str
    email: str
    id_token: str
    display_name: str
    photo_url: str


class CredentialsError(AppError):
    """Wrong email or password. Deliberately indistinguishable from each other."""

    def __init__(self, message: str = "Email or password is incorrect.") -> None:
        super().__init__(
            message, status_code=status.HTTP_401_UNAUTHORIZED, code="invalid_credentials"
        )


# Identity Toolkit's code -> what a person should read. Anything absent is
# treated as an upstream failure, because an unmapped code is one we have not
# decided is safe to reveal.
_MESSAGES: dict[str, tuple[str, int, str]] = {
    "EMAIL_EXISTS": (
        "That email already has an account. Try signing in instead.",
        status.HTTP_409_CONFLICT,
        "email_taken",
    ),
    "INVALID_EMAIL": (
        "That email address does not look right.",
        status.HTTP_400_BAD_REQUEST,
        "invalid_email",
    ),
    "WEAK_PASSWORD": (
        "Pick a password of at least six characters.",
        status.HTTP_400_BAD_REQUEST,
        "weak_password",
    ),
    "USER_DISABLED": (
        "This account is disabled.",
        status.HTTP_403_FORBIDDEN,
        "account_disabled",
    ),
    "TOO_MANY_ATTEMPTS_TRY_LATER": (
        "Too many attempts. Wait a moment and try again.",
        status.HTTP_429_TOO_MANY_REQUESTS,
        "rate_limited",
    ),
    "OPERATION_NOT_ALLOWED": (
        "That sign-in method is switched off for this project.",
        status.HTTP_501_NOT_IMPLEMENTED,
        "not_configured",
    ),
    "ADMIN_ONLY_OPERATION": (
        "This project does not allow new sign-ups.",
        status.HTTP_403_FORBIDDEN,
        "signup_disabled",
    ),
}

# Codes that mean "no such account" or "wrong password". All collapse to one
# message: telling a caller which of the two it was hands them an oracle for
# whether an address is registered.
_CREDENTIAL_CODES = frozenset(
    {
        "EMAIL_NOT_FOUND",
        "INVALID_PASSWORD",
        "INVALID_LOGIN_CREDENTIALS",
        "MISSING_PASSWORD",
    }
)


def _translate(code: str) -> AppError:
    if code in _CREDENTIAL_CODES:
        return CredentialsError()

    # Codes arrive as "TOO_MANY_ATTEMPTS_TRY_LATER : reason", so match the head.
    head = code.split(":", 1)[0].strip()
    known = _MESSAGES.get(head)
    if known is not None:
        message, http_status, app_code = known
        return AppError(message, status_code=http_status, code=app_code)

    logger.warning("Unmapped Identity Toolkit code: %s", head)
    return UpstreamError("Sign-in is unavailable right now.")


def _api_key(settings: Settings) -> str:
    if settings.firebase_web_api_key is None:
        raise AppError(
            "Sign-in is not configured on the server.",
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            code="not_configured",
        )
    return settings.firebase_web_api_key.get_secret_value()


async def _call(
    endpoint: str, payload: dict[str, Any], settings: Settings
) -> dict[str, Any]:
    """One Identity Toolkit request, with its errors already translated."""
    url = f"{_BASE}:{endpoint}"

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.post(
                url, params={"key": _api_key(settings)}, json=payload
            )
    except httpx.HTTPError as exc:
        logger.warning("Identity Toolkit unreachable: %s", type(exc).__name__)
        raise UpstreamError("Sign-in is unavailable right now.") from exc

    body: dict[str, Any] = response.json() if response.content else {}

    if response.is_success:
        return body

    code = str(body.get("error", {}).get("message", "")) or "UNKNOWN"
    raise _translate(code)


def _result(body: dict[str, Any]) -> IdentityResult:
    return IdentityResult(
        uid=str(body.get("localId", "")),
        email=str(body.get("email", "")),
        id_token=str(body.get("idToken", "")),
        display_name=str(body.get("displayName", "")),
        photo_url=str(body.get("photoUrl", "")),
    )


async def sign_up(email: str, password: str, settings: Settings) -> IdentityResult:
    """Create an account. The caller is not signed in by this alone — the route
    mints a session from the returned token."""
    body = await _call(
        "signUp",
        {"email": email, "password": password, "returnSecureToken": True},
        settings,
    )
    return _result(body)


async def sign_in(email: str, password: str, settings: Settings) -> IdentityResult:
    body = await _call(
        "signInWithPassword",
        {"email": email, "password": password, "returnSecureToken": True},
        settings,
    )
    return _result(body)


async def sign_in_with_google(
    id_token: str, request_uri: str, settings: Settings
) -> IdentityResult:
    """Exchange a Google ID token for a Firebase account.

    Creates the Firebase user on first sign-in and links it to the same account
    when the address already exists with a password, which is what stops one
    person ending up with two accounts for one email.
    """
    body = await _call(
        "signInWithIdp",
        {
            "postBody": f"id_token={id_token}&providerId=google.com",
            "requestUri": request_uri,
            "returnSecureToken": True,
            "returnIdpCredential": True,
        },
        settings,
    )
    return _result(body)


async def exchange_custom_token(custom_token: str, settings: Settings) -> str:
    """Turn an Admin-SDK custom token into an ID token.

    Needed because ``sendOobCode`` for verification wants an ID token and the
    Admin SDK cannot produce one. The result is used immediately, inside this
    process, and never handed to a browser.
    """
    body = await _call(
        "signInWithCustomToken",
        {"token": custom_token, "returnSecureToken": True},
        settings,
    )
    return str(body.get("idToken", ""))


async def set_display_name(
    id_token: str, display_name: str, settings: Settings
) -> None:
    """Set the name on the auth record itself, so it reaches token claims."""
    await _call(
        "update",
        {"idToken": id_token, "displayName": display_name, "returnSecureToken": False},
        settings,
    )


async def verification_link(id_token: str, settings: Settings) -> str:
    """The confirm-your-address link, for the branded email to carry.

    Asking for the link rather than letting Identity Toolkit send it is what
    keeps Firebase's unbranded default template from going out alongside ours.
    """
    body = await _call(
        "sendOobCode",
        {"requestType": "VERIFY_EMAIL", "idToken": id_token, "returnOobLink": True},
        settings,
    )
    return str(body.get("oobLink", ""))


async def send_password_reset(email: str, settings: Settings) -> None:
    """Ask Identity Toolkit to send its password reset email.

    Unlike verification, this one is left to Firebase's own template: a reset
    has to work for someone who cannot sign in, so there is no ID token to
    fetch a link with.
    """
    await _call(
        "sendOobCode", {"requestType": "PASSWORD_RESET", "email": email}, settings
    )
