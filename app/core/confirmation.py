"""Email confirmation links, signed rather than stored.

A confirmation is a uid and an expiry with a signature over both. Nothing is
written down until someone clicks: there is no pending-confirmations table to
clean up, no row to leak, and a horizontally-scaled deployment needs no shared
store for something that lives twenty-four hours.

Why this exists at all, when Firebase already has email verification: Google
sign-in arrives pre-verified — the OAuth handshake *is* the proof the person
controls that address — so Firebase sets ``email_verified`` itself and there is
nothing left to confirm. Holding those accounts at the gate needs a flag this
service owns, which is what ``confirmedAt`` on the account record is.

The signing key is the service account's private key, which is already the
credential whose compromise would end this project anyway. Using it means no
extra secret to configure, rotate, or forget to set.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import time

from fastapi import status

from app.core.config import Settings
from app.core.errors import AppError

logger = logging.getLogger(__name__)

# Long enough that an email sitting unread overnight still works, short enough
# that a link forwarded or left in an old inbox stops being a way in.
TTL_SECONDS = 24 * 60 * 60


class ConfirmationError(AppError):
    def __init__(self, message: str) -> None:
        super().__init__(
            message, status_code=status.HTTP_400_BAD_REQUEST, code="bad_confirmation"
        )


def _key(settings: Settings) -> bytes:
    info = settings.service_account_info()
    if info is None:  # pragma: no cover - startup refuses this already
        raise AppError(
            "Email confirmation is not configured on the server.",
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            code="not_configured",
        )
    return str(info["private_key"]).encode()


def _sign(payload: str, settings: Settings) -> str:
    digest = hmac.new(_key(settings), payload.encode(), hashlib.sha256).digest()
    # URL-safe and unpadded: this goes in a query string, and '=' there is noise
    # that some mail clients mangle.
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def issue(uid: str, settings: Settings) -> str:
    """A token confirming this uid, good for a day."""
    payload = f"{uid}.{int(time.time()) + TTL_SECONDS}"
    return f"{payload}.{_sign(payload, settings)}"


def redeem(token: str, settings: Settings) -> str:
    """Check a token and return the uid it confirms.

    Every failure says the same thing. Distinguishing "expired" from "forged"
    tells someone probing which half of the token to keep working on.
    """
    parts = token.rsplit(".", 1)
    if len(parts) != 2:
        raise ConfirmationError("That confirmation link is not valid.")

    payload, signature = parts
    # compare_digest, not ==: a timing comparison on a signature is a real
    # forgery oracle given enough attempts.
    if not hmac.compare_digest(signature, _sign(payload, settings)):
        raise ConfirmationError("That confirmation link is not valid.")

    uid, _, expires = payload.rpartition(".")
    if not uid:
        raise ConfirmationError("That confirmation link is not valid.")

    try:
        expires_at = int(expires)
    except ValueError as exc:
        raise ConfirmationError("That confirmation link is not valid.") from exc

    if time.time() > expires_at:
        raise ConfirmationError("That confirmation link has expired. Ask for a new one.")

    return uid
