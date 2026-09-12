"""Application-level encryption.

Firestore and the Realtime Database already encrypt at rest with keys Google
holds. This is the layer above that: it keeps values unreadable to anyone who
reaches the data rather than the application — the console, a backup, a leaked
service account, a rule that turns out to be too generous.

It does **not** protect against this service being compromised. The key is here,
so anything that runs code here can read anything. Be honest about that when
deciding what it is worth.

Two properties make it safe to roll out onto a live database:

  * **Self-describing.** A sealed value carries a version prefix. ``unseal``
    passes anything without that prefix straight through, so documents written
    before the key existed keep working and a gradual migration needs no flag
    day.
  * **Fail-closed on read of a sealed value.** If a value claims to be sealed
    and cannot be opened, that is a wrong key or tampering, and it raises
    rather than silently showing ciphertext as if it were text.

AES-256-GCM: authenticated, so a modified ciphertext is rejected rather than
decrypting to rubbish. The nonce is random per value and stored alongside it.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.errors import AppError

logger = logging.getLogger(__name__)

# Version prefix. Changing the scheme means a new tag, and unseal can then keep
# reading the old one while new writes use the new — which is what makes key
# rotation possible without a migration window.
_PREFIX = "enc.v1."

# GCM's recommended nonce length. Random per value, never reused with one key.
_NONCE_BYTES = 12


class EncryptionError(AppError):
    def __init__(self, message: str) -> None:
        super().__init__(message, status_code=500, code="encryption_error")


def _key() -> bytes | None:
    """The data key, or None when encryption is switched off.

    Off means new writes are plaintext and reads still work — the point of the
    prefix. It is the correct behaviour for a developer running against an
    empty emulator, and the wrong one for production, which is why
    ``warn_if_disabled`` exists and startup calls it.
    """
    raw = os.environ.get("DATA_ENCRYPTION_KEY", "").strip()
    if not raw:
        return None

    try:
        key = base64.urlsafe_b64decode(raw)
    except (ValueError, TypeError) as exc:
        raise EncryptionError("DATA_ENCRYPTION_KEY is not valid base64.") from exc

    if len(key) != 32:
        raise EncryptionError(
            f"DATA_ENCRYPTION_KEY must decode to 32 bytes, got {len(key)}."
        )
    return key


def is_enabled() -> bool:
    return _key() is not None


def warn_if_disabled(*, production: bool) -> None:
    """Say so at startup, loudly, rather than letting it be discovered later."""
    if is_enabled():
        return

    message = (
        "DATA_ENCRYPTION_KEY is not set: values are being written in plaintext. "
        "Generate one with: python -c "
        "\"import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())\""
    )
    if production:
        logger.error(message)
    else:
        logger.warning(message)


def seal(value: Any) -> Any:
    """Encrypt one JSON-serialisable value.

    The value is JSON-encoded first, so a number comes back a number and a list
    comes back a list. Encrypting the string form of a value and hoping the
    caller re-parses it correctly is how types quietly rot.

    Returns the value untouched when encryption is off, and for None — an
    absent field should stay absent rather than becoming an opaque blob that
    every ``is None`` check then misses.
    """
    key = _key()
    if key is None or value is None:
        return value

    nonce = os.urandom(_NONCE_BYTES)
    plaintext = json.dumps(value, default=str).encode()
    sealed = AESGCM(key).encrypt(nonce, plaintext, None)

    return _PREFIX + base64.urlsafe_b64encode(nonce + sealed).decode()


def unseal(value: Any) -> Any:
    """Decrypt a value sealed by ``seal``; pass anything else through.

    Passing plaintext through is what lets this be switched on against a
    database that already holds data. A value that *claims* to be sealed and
    will not open is a different matter and raises: showing ciphertext to
    somebody as though it were their note would be worse than an error.
    """
    if not isinstance(value, str) or not value.startswith(_PREFIX):
        return value

    key = _key()
    if key is None:
        raise EncryptionError(
            "Found encrypted data but DATA_ENCRYPTION_KEY is not set."
        )

    try:
        raw = base64.urlsafe_b64decode(value[len(_PREFIX) :])
        opened = AESGCM(key).decrypt(raw[:_NONCE_BYTES], raw[_NONCE_BYTES:], None)
        return json.loads(opened)
    except (InvalidTag, ValueError, TypeError) as exc:
        logger.error("Could not decrypt a stored value: %s", type(exc).__name__)
        raise EncryptionError(
            "Stored data could not be decrypted. The key may have changed."
        ) from exc


def seal_fields(document: dict[str, Any], fields: frozenset[str]) -> dict[str, Any]:
    """Seal the named fields of a document, leaving the rest alone.

    The rest is not an oversight. Anything Firestore has to filter, order or
    range over must stay readable to Firestore — ``uid`` because every query is
    scoped by it, ``createdAt`` because every list is ordered by it. Encrypting
    those would not hide much and would break paging entirely.
    """
    return {
        key: seal(value) if key in fields else value
        for key, value in document.items()
    }


def unseal_fields(document: dict[str, Any], fields: frozenset[str]) -> dict[str, Any]:
    return {
        key: unseal(value) if key in fields else value
        for key, value in document.items()
    }
