"""Per-conversation keys for browser-side chat encryption.

Derived, never stored. The key for a conversation is an HMAC of its thread id
under a secret only this service holds, so:

  * both participants get the same key, because the thread id is the two uids
    sorted and is therefore the same from either side;
  * nobody else can compute it, because the HMAC secret never leaves here;
  * there is no key table to keep, back up, or leak, and adding a conversation
    costs nothing.

The trade is that this service can derive any conversation's key and so could
read any conversation. That is not end-to-end encryption, and calling it that
would be a lie. What it buys is that the stored messages are unreadable to
anyone who reaches the database rather than the application.
"""

from __future__ import annotations

import base64
import hashlib
import hmac

from app.core.config import get_settings
from app.core.errors import AppError


def _secret() -> bytes:
    """The HMAC secret.

    The service account private key, reused. It is already the credential whose
    compromise would end this project, so deriving from it adds no new thing to
    protect — and it means no extra variable that can be forgotten, or worse,
    changed. Changing it would make every stored message unreadable.
    """
    info = get_settings().service_account_info()
    if info is None:  # pragma: no cover - startup refuses this already
        raise AppError(
            "Chat encryption is not configured on the server.",
            status_code=501,
            code="not_configured",
        )
    return str(info["private_key"]).encode()


def thread_id(a: str, b: str) -> str:
    """Sorted, so both participants derive the same id — and the same key."""
    return "_".join(sorted((a, b)))


def derive_thread_key(a: str, b: str) -> str:
    """A base64 AES-256 key for the conversation between two people."""
    digest = hmac.new(_secret(), thread_id(a, b).encode(), hashlib.sha256).digest()
    # urlsafe so it survives a query string or a JSON body without escaping.
    return base64.urlsafe_b64encode(digest).decode()
