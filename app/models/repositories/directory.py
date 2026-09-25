"""The account directory: who other traders are, for contact search.

Kept apart from the account record on purpose. A contact lookup has to read
*other* people's rows, and the account record holds things nobody else should
see — plan, opening balance, bio. A directory entry is the deliberate public
face of an account: who they are, how to recognise them, nothing more.

That separation used to be enforced by Firestore rules, because the browser
read this collection itself. It is now enforced by this file being the only
code that reads it, and by returning a typed shape rather than a document.
"""

from __future__ import annotations

from typing import Any

from google.cloud.firestore_v1 import SERVER_TIMESTAMP

from app.core.security import CurrentUser
from app.db.firestore import get_client
from app.models.schemas.directory import DirectoryEntry

_DIRECTORY = "directory"

# How many matches a search will return. Small on purpose: this is a lookup for
# someone you already know the address of, not a browsable list of every user.
RESULT_LIMIT = 6

# Below this a search is refused outright. A one-character prefix would return
# an arbitrary slice of the user base, which is enumeration however it is
# capped.
MIN_QUERY = 3


def _collection() -> Any:
    return get_client().collection(_DIRECTORY)


def _to_entry(uid: str, data: dict[str, Any]) -> DirectoryEntry:
    return DirectoryEntry(
        uid=uid,
        email=str(data.get("email", "")),
        display_name=str(data.get("displayName", "")),
        photo_url=str(data.get("photoURL", "")),
    )


def publish(user: CurrentUser) -> None:
    """Mirror an account into the directory.

    Called on every sign-in and after a profile save, so a changed name or
    avatar reaches everyone who has this person in their contacts.

    ``emailLower`` exists because Firestore range queries are case-sensitive
    and people do not type their addresses consistently.
    """
    email = user.email or ""

    _collection().document(user.uid).set(
        {
            "uid": user.uid,
            "email": email,
            "emailLower": email.lower(),
            "displayName": user.name or "",
            "photoURL": user.picture or "",
            "updatedAt": SERVER_TIMESTAMP,
        },
        merge=True,
    )


def publish_fields(uid: str, *, display_name: str, photo_url: str) -> None:
    """Keep the searchable copy in step with a profile edit."""
    _collection().document(uid).set(
        {
            "displayName": display_name,
            "photoURL": photo_url,
            "updatedAt": SERVER_TIMESTAMP,
        },
        merge=True,
    )


def search(term: str, *, exclude_uid: str) -> list[DirectoryEntry]:
    """Find accounts whose email starts with ``term``.

    A prefix range rather than an equality match, so the client's dropdown
    fills in as an address is typed. ``\\uf8ff`` is the last code point
    Firestore orders against, which bounds the range to the prefix.

    The caller is filtered out here rather than by the client: adding yourself
    is not a conversation, and a client-side filter is a suggestion.
    """
    needle = term.strip().lower()
    if len(needle) < MIN_QUERY:
        return []

    # One extra, so removing the caller cannot leave a short page.
    snapshot = (
        _collection()
        .order_by("emailLower")
        .start_at({"emailLower": needle})
        .end_at({"emailLower": f"{needle}"})
        .limit(RESULT_LIMIT + 1)
        .get()
    )

    found = [
        _to_entry(doc.id, doc.to_dict() or {})
        for doc in snapshot
        if doc.id != exclude_uid
    ]
    return found[:RESULT_LIMIT]


def get_many(uids: list[str]) -> dict[str, DirectoryEntry]:
    """Entries for a set of uids, for resolving a contact list into people."""
    if not uids:
        return {}

    references = [_collection().document(uid) for uid in uids]
    return {
        doc.id: _to_entry(doc.id, doc.to_dict() or {})
        for doc in get_client().get_all(references)
        if doc.exists
    }
