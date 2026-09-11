"""Conversations between traders.

No route takes a thread id. A conversation is addressed by the person at the
other end of it, and the thread is derived from that uid plus the caller's —
which is what makes it impossible to name a conversation you are not in.

Presence is a heartbeat rather than a connection: listing contacts marks the
caller as here. That survives a dropped request and a closed laptop without a
disconnect handler, at the cost of being accurate to about a minute.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Query, Request, status
from fastapi.responses import StreamingResponse

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

logger = logging.getLogger(__name__)

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


# ------------------------------------------------------------------- stream

# How long a quiet connection waits before sending a comment line. Proxies and
# load balancers close idle connections; a heartbeat keeps this one open and
# lets the client notice a dead link rather than waiting forever on silence.
_HEARTBEAT_SECONDS = 20.0

# A burst of changes while nobody is reading should not grow without bound.
# Every event says the same thing — "refetch" — so dropping extras costs
# nothing: the client reads current state when it acts on any one of them.
_QUEUE_SIZE = 64


@router.get(
    "/stream",
    summary="Live conversation updates",
    response_class=StreamingResponse,
    # The stream is one long request; counting it against the per-minute budget
    # would spend the whole allowance on opening the dock once.
    dependencies=[],
)
async def stream(user: ReadUser, request: Request) -> StreamingResponse:
    """Server-sent events: one line whenever a conversation changes.

    Carries a nudge, not the data. Each event names the person whose thread
    moved and nothing else; the client refetches through the ordinary endpoints.
    That keeps every authorisation and serialisation decision in one place
    rather than duplicating it into a second, harder-to-audit path — and a
    dropped event costs nothing, because the next fetch reads current state
    regardless of how many nudges preceded it.

    The Firestore listener runs on a gRPC background thread. It never touches
    the event loop directly: it hands work over with ``call_soon_threadsafe``,
    which is the only safe way across that boundary.
    """
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[str] = asyncio.Queue(maxsize=_QUEUE_SIZE)

    def on_change(snapshots: Any, changes: Any, read_time: Any) -> None:
        for snapshot in snapshots:
            them = repo.other_member(snapshot.to_dict() or {}, user.uid)
            if them is None:
                continue

            payload = json.dumps({"uid": them})
            # put_nowait raises when full; a full queue means the client is
            # behind, and it will refetch anyway. Dropping is correct here.
            loop.call_soon_threadsafe(_offer, queue, payload)

    watch = repo.watch_threads(user.uid, on_change)

    async def events() -> AsyncIterator[str]:
        try:
            # An immediate comment so the browser marks the stream open rather
            # than sitting in "connecting" until the first change.
            yield ": open\n\n"

            while True:
                try:
                    payload = await asyncio.wait_for(
                        queue.get(), timeout=_HEARTBEAT_SECONDS
                    )
                    yield f"event: change\ndata: {payload}\n\n"
                except TimeoutError:
                    yield ": keep-alive\n\n"

                if await request.is_disconnected():
                    break
        finally:
            # Whatever ends this — disconnect, cancellation, error — the
            # listener has to go with it, or a closed tab leaves a Firestore
            # subscription running for the life of the process.
            watch.unsubscribe()

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "Connection": "keep-alive",
            # nginx and several CDNs buffer proxied responses by default, which
            # would hold every event until the connection closed.
            "X-Accel-Buffering": "no",
        },
    )


def _offer(queue: asyncio.Queue[str], payload: str) -> None:
    """Enqueue if there is room. Called on the event loop, never the gRPC thread."""
    try:
        queue.put_nowait(payload)
    except asyncio.QueueFull:  # pragma: no cover - only under a burst
        logger.debug("Chat stream queue full; dropping a nudge.")
