"""What the chat feature still needs from the API.

Two endpoints, and deliberately no messages. Conversations live in the Realtime
Database and are read straight from the browser over a websocket, because a
message has to land in well under a second and a request proxied through this
service cannot do that. See trades/src/lib/chat.ts, and database.rules.json for
what a browser is allowed to reach there.

What stays here is what the browser must not decide for itself: who it is, and
who else exists.
"""

from __future__ import annotations

from fastapi import APIRouter, Query
from firebase_admin import auth as firebase_auth

from app.api.deps import ReadUser, StandardRateLimit
from app.repositories import directory as directory_repo
from app.schemas.chat import ChatToken
from app.schemas.common import ErrorResponse
from app.schemas.directory import DirectoryResults

router = APIRouter(
    prefix="/chat",
    tags=["chat"],
    dependencies=[StandardRateLimit],
    responses={401: {"model": ErrorResponse, "description": "Not signed in"}},
)


@router.get("/token", response_model=ChatToken, summary="Credential for live chat")
async def chat_token(user: ReadUser) -> ChatToken:
    """Mint a Firebase custom token so the browser can open a live connection.

    The session cookie stays the source of truth. A caller gets a token only for
    the uid their verified session already names — there is no parameter here,
    and nothing a client sends influences whose token it receives.

    What the token then permits is bounded by database.rules.json, which covers
    chat and nothing else. It cannot reach the journal: that lives in Firestore,
    whose rules deny every direct client read.
    """
    token = firebase_auth.create_custom_token(user.uid)
    return ChatToken(token=token.decode())


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

    Kept on this side rather than in the Realtime Database on purpose: a client
    that could query the whole user list could enumerate it. Here the query is
    bounded, the caller is filtered out, and only four fields come back.
    """
    return DirectoryResults(results=directory_repo.search(q, exclude_uid=user.uid))
