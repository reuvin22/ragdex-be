"""Conversations between traders.

No route takes a thread id. A conversation is addressed by the person at the
other end of it, and the thread is derived from that uid plus the caller's —
which is what makes it impossible to name a conversation you are not in.

Presence is a heartbeat rather than a connection: listing contacts marks the
caller as here. That survives a dropped request and a closed laptop without a
disconnect handler, at the cost of being accurate to about a minute.
"""

from __future__ import annotations

from fastapi import APIRouter, Query, status

from app.api.deps import ReadUser, StandardRateLimit, WriteUser
from app.repositories import chat as repo
from app.repositories import directory as directory_repo
from app.schemas.chat import (
    ChatMessage,
    Contact,
    ContactList,
    NewContact,
    NewMessage,
    Thread,
)
from app.schemas.common import ErrorResponse
from app.schemas.directory import DirectoryResults

router = APIRouter(
    prefix="/chat",
    tags=["chat"],
    dependencies=[StandardRateLimit],
    responses={401: {"model": ErrorResponse, "description": "Not signed in"}},
)


@router.get("/contacts", response_model=ContactList, summary="Your conversations")
async def list_contacts(user: ReadUser) -> ContactList:
    repo.touch_presence(user.uid)
    return ContactList(contacts=repo.list_contacts(user.uid))


@router.post(
    "/contacts",
    response_model=Contact,
    status_code=status.HTTP_201_CREATED,
    summary="Start a conversation",
)
async def add_contact(user: WriteUser, payload: NewContact) -> Contact:
    """Open a conversation with someone found through the directory.

    One document holds both participants, so this makes the conversation
    visible from both ends without writing to anyone else's data.
    """
    repo.add_contact(user.uid, payload.uid)

    found = next(
        (c for c in repo.list_contacts(user.uid) if c.person.uid == payload.uid), None
    )
    if found is None:  # pragma: no cover - the write above just created it
        return Contact(person=directory_repo.get_many([payload.uid])[payload.uid])
    return found


@router.get(
    "/threads/{uid}",
    response_model=Thread,
    summary="One conversation",
    responses={404: {"model": ErrorResponse, "description": "No such conversation"}},
)
async def read_thread(user: ReadUser, uid: str) -> Thread:
    repo.touch_presence(user.uid)
    return repo.read_thread(user.uid, uid)


@router.post(
    "/threads/{uid}/messages",
    response_model=ChatMessage,
    status_code=status.HTTP_201_CREATED,
    summary="Send a message",
)
async def send_message(user: WriteUser, uid: str, payload: NewMessage) -> ChatMessage:
    """The body carries text and nothing else.

    Sender and timestamp are set server-side: a client that could supply either
    could forge a message from the person it is talking to, or backdate one
    under their read marker so it never showed as unread.
    """
    repo.touch_presence(user.uid)
    return repo.send_message(user.uid, uid, payload.text)


@router.post(
    "/threads/{uid}/seen",
    response_model=Thread,
    summary="Mark a conversation read",
)
async def mark_seen(user: WriteUser, uid: str) -> Thread:
    """Stamps the caller's read marker and returns the thread as it now stands,
    so the client does not need a second call to see the effect."""
    repo.mark_seen(user.uid, uid)
    return repo.read_thread(user.uid, uid)


@router.get(
    "/directory",
    response_model=DirectoryResults,
    summary="Find a trader by email",
)
async def search_directory(
    user: ReadUser,
    q: str = Query(default="", max_length=200, description="Email address or prefix"),
) -> DirectoryResults:
    """Search is by email prefix, with a floor on how short the prefix may be.

    A one- or two-character search would return an arbitrary slice of the user
    base, which is enumeration however small the page is. The floor lives in
    the repository so it holds for every caller of it.
    """
    return DirectoryResults(results=directory_repo.search(q, exclude_uid=user.uid))
