"""Conversations, in Firestore.

    /threads/{threadId}                  members, reads, last message
    /threads/{threadId}/messages/{id}    one message
    /presence/{uid}                      last seen, for the online dot

A thread id is the two participants' uids sorted and joined with '_', so both
sides derive the same id without a lookup and a pair can only ever have one
conversation.

Every function takes the caller's uid first and derives the thread from it.
There is no code path here that reaches a thread the caller is not in — the
Admin SDK bypasses security rules entirely, so that discipline is the only
thing standing between one trader and another's messages.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from google.cloud.firestore_v1 import SERVER_TIMESTAMP, FieldFilter, Query

from app.core.errors import AppError, NotFoundError
from app.db.firestore import get_client
from app.schemas.chat import ChatMessage, Contact, Thread
from app.schemas.directory import DirectoryEntry

from . import directory as directory_repo

_THREADS = "threads"
_MESSAGES = "messages"
_PRESENCE = "presence"

# How much history one request returns. A dock, not an archive.
HISTORY = 100

# Presence is a heartbeat, not a connection: someone is online if they have
# been seen recently. That survives a dropped socket and a closed laptop
# without needing a disconnect handler.
_ONLINE_WINDOW = timedelta(seconds=75)


def thread_id(a: str, b: str) -> str:
    return "_".join(sorted((a, b)))


def _threads() -> Any:
    return get_client().collection(_THREADS)


def _thread_ref(me: str, them: str) -> Any:
    if me == them:
        raise AppError("You cannot message yourself.", code="invalid_contact")
    return _threads().document(thread_id(me, them))


def _to_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    return None


def _to_message(doc_id: str, data: dict[str, Any]) -> ChatMessage:
    return ChatMessage(
        id=doc_id,
        sender=str(data.get("from", "")),
        text=str(data.get("text", "")),
        # A message read back the instant it was written can still have a
        # pending server timestamp. Falling back to now keeps it ordered last
        # rather than dropping it.
        sent_at=_to_datetime(data.get("at")) or datetime.now(UTC),
    )


def _person(uid: str, entries: dict[str, DirectoryEntry]) -> DirectoryEntry:
    """Who a uid belongs to, or a placeholder when the entry is gone.

    A deleted account should leave a readable conversation behind, not an
    exception in the middle of a list.
    """
    return entries.get(uid) or DirectoryEntry(uid=uid)


# ------------------------------------------------------------------ contacts


def add_contact(me: str, them: str) -> None:
    """Open the conversation between two people.

    One document, with both uids in it, which is what makes the conversation
    visible from both ends. There is no separate per-user contact list to keep
    in step, and no write to another person's data.
    """
    if me == them:
        raise AppError("You cannot add yourself as a contact.", code="invalid_contact")

    if not directory_repo.get_many([them]):
        raise NotFoundError("No account with that id.")

    reference = _thread_ref(me, them)
    if reference.get().exists:
        return

    reference.set(
        {
            "members": sorted((me, them)),
            "createdAt": SERVER_TIMESTAMP,
            "reads": {},
            "lastText": "",
        }
    )


def list_contacts(me: str) -> list[Contact]:
    """Everyone the caller has a conversation with, newest first."""
    snapshot = (
        _threads().where(filter=FieldFilter("members", "array_contains", me)).get()
    )

    # Paired in one pass. Building the uid list separately and zipping it back
    # would silently misalign the moment one document is malformed.
    pairs: list[tuple[str, dict[str, Any], str]] = []
    for doc in snapshot:
        data = doc.to_dict() or {}
        others = [uid for uid in data.get("members", []) if uid != me]
        if len(others) == 1:
            pairs.append((doc.id, data, others[0]))

    uids = [them for _, _, them in pairs]
    entries = directory_repo.get_many(uids)
    online = presence_of(uids)

    contacts: list[Contact] = []
    for doc_id, data, them in pairs:
        reads = data.get("reads") or {}
        contacts.append(
            Contact(
                person=_person(them, entries),
                unread=_unread_count(doc_id, _to_datetime(reads.get(me))),
                last_text=str(data.get("lastText", "")),
                last_at=_to_datetime(data.get("lastAt")),
                seen_at=_to_datetime(reads.get(them)),
                online=them in online,
            )
        )

    # Sorted here rather than in the query: ordering by lastAt alongside an
    # array-contains filter needs a composite index, and a contact list is
    # small enough that sorting it is free.
    contacts.sort(key=lambda c: (c.last_at is not None, c.last_at), reverse=True)
    return contacts


def _unread_count(doc_id: str, seen_at: datetime | None) -> int:
    """How many messages arrived after the caller last looked.

    An aggregation, so the messages themselves are never fetched to be counted.

    There is deliberately no "and not from me" filter. Sending stamps the
    sender's own read marker (see ``send_message``), so anything past that
    marker came from the other person — and a second inequality, on a different
    field, would need a composite index for what a write already guarantees.
    """
    query = _threads().document(doc_id).collection(_MESSAGES)
    if seen_at is not None:
        query = query.where(filter=FieldFilter("at", ">", seen_at))

    result = query.count().get()
    return int(result[0][0].value) if result else 0


# ------------------------------------------------------------------- thread


def read_thread(me: str, them: str) -> Thread:
    reference = _thread_ref(me, them)
    snapshot = reference.get()
    if not snapshot.exists:
        raise NotFoundError("No conversation with that person.")

    data = snapshot.to_dict() or {}
    if me not in data.get("members", []):
        # Unreachable through the id derivation above, and checked anyway: this
        # is the assertion that the derivation is what it claims to be.
        raise NotFoundError("No conversation with that person.")

    documents = (
        reference.collection(_MESSAGES)
        .order_by("at", direction=Query.DESCENDING)
        .limit(HISTORY)
        .get()
    )
    messages = [_to_message(doc.id, doc.to_dict() or {}) for doc in reversed(documents)]

    reads = data.get("reads") or {}
    entries = directory_repo.get_many([them])

    return Thread(
        person=_person(them, entries),
        messages=messages,
        seen_at=_to_datetime(reads.get(them)),
        online=them in presence_of([them]),
    )


def send_message(me: str, them: str, text: str) -> ChatMessage:
    """Append a message.

    The sender and the clock are both set here. A caller supplies the text and
    nothing else — a client that could set ``from`` could forge a message from
    the person it is talking to.
    """
    reference = _thread_ref(me, them)
    if not reference.get().exists:
        # Messaging someone implies a conversation with them.
        add_contact(me, them)

    _, written = reference.collection(_MESSAGES).add(
        {"from": me, "text": text, "at": SERVER_TIMESTAMP}
    )

    # Two things at once: the preview the contact list renders without reading
    # a subcollection, and the sender's own read marker — you are looking at a
    # thread when you send to it, and stamping here is what lets the unread
    # count be a single-inequality query.
    reference.set(
        {
            "lastText": text[:120],
            "lastAt": SERVER_TIMESTAMP,
            "lastFrom": me,
            "reads": {me: SERVER_TIMESTAMP},
        },
        merge=True,
    )

    return _to_message(written.id, written.get().to_dict() or {})


def mark_seen(me: str, them: str) -> datetime | None:
    """Stamp the thread as read, now.

    One timestamp per participant is the whole read-receipt mechanism: a
    message counts as seen once the other person's stamp is at or past it, so a
    single write covers every message it arrived after.
    """
    reference = _thread_ref(me, them)
    if not reference.get().exists:
        raise NotFoundError("No conversation with that person.")

    reference.set({"reads": {me: SERVER_TIMESTAMP}}, merge=True)

    reads = (reference.get().to_dict() or {}).get("reads") or {}
    return _to_datetime(reads.get(me))


# ----------------------------------------------------------------- presence


def touch_presence(uid: str) -> None:
    """Record that someone is here. Called as a side effect of polling."""
    get_client().collection(_PRESENCE).document(uid).set(
        {"at": SERVER_TIMESTAMP}, merge=True
    )


def presence_of(uids: list[str]) -> set[str]:
    """Which of these people have been seen inside the online window."""
    if not uids:
        return set()

    collection = get_client().collection(_PRESENCE)
    references = [collection.document(uid) for uid in uids]
    cutoff = datetime.now(UTC) - _ONLINE_WINDOW

    online = set()
    for doc in get_client().get_all(references):
        if not doc.exists:
            continue
        at = _to_datetime((doc.to_dict() or {}).get("at"))
        if at is not None and at > cutoff:
            online.add(doc.id)

    return online


# ------------------------------------------------------------------ watching


def watch_threads(me: str, on_change: Any) -> Any:
    """Subscribe to every conversation this person is in.

    One listener covers all of them: a thread document carries ``lastAt`` and
    ``reads``, and both a new message and a read receipt write to it — so the
    document changing is exactly the signal "something happened in here", with
    no listener per conversation and no collection-group query.

    ``on_change`` is called from a gRPC background thread, not the event loop.
    Whatever it does has to be thread-safe; the route hands it a queue.
    """
    query = _threads().where(filter=FieldFilter("members", "array_contains", me))
    return query.on_snapshot(on_change)


def other_member(data: dict[str, Any], me: str) -> str | None:
    """Who the other participant is, from a raw thread document."""
    others = [uid for uid in data.get("members", []) if uid != me]
    return others[0] if len(others) == 1 else None
