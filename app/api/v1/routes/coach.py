"""AI coach conversation.

The journal is read here with the Admin SDK and never accepted from the
client, so the coach can only ever talk about trades the trader actually
logged. A forged history changes the shape of the conversation, not the facts
in it.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import AppSettings, CoachRateLimit, ReadUser, WriteUser
from app.repositories import conversations as conversations_repo
from app.repositories import profiles as profiles_repo
from app.repositories import trades as trades_repo
from app.schemas.coach import (
    HISTORY_LIMIT,
    CoachConversation,
    CoachReply,
    CoachRequest,
)
from app.schemas.common import ErrorResponse
from app.services import coach as coach_service
from app.services.stats import summarise

router = APIRouter(
    prefix="/coach",
    tags=["coach"],
    dependencies=[CoachRateLimit],
    responses={
        401: {"model": ErrorResponse},
        429: {"model": ErrorResponse, "description": "Rate limited"},
        501: {"model": ErrorResponse, "description": "Coach not configured"},
        502: {"model": ErrorResponse, "description": "Model unavailable"},
        504: {"model": ErrorResponse, "description": "Model timed out"},
    },
)


@router.post("/chat", response_model=CoachReply, summary="Ask the coach")
async def chat(
    user: WriteUser, payload: CoachRequest, settings: AppSettings
) -> CoachReply:
    trades = trades_repo.all_trades(user.uid)
    summary = summarise(trades) if trades else None

    # Read here rather than accepted from the client, so the coach remembers
    # across a refresh and cannot be told it said something it did not.
    history = conversations_repo.load_turns(user.uid)

    completion = await coach_service.ask(
        history=history[-HISTORY_LIMIT:],
        message=payload.message,
        summary=summary,
        language=payload.language,
        display_name=user.name or "",
        settings=settings,
        image=payload.image,
        # Their own capital, risk limits and rules, so the coach measures
        # execution against a standard they set rather than guessing at one.
        profile=profiles_repo.get_profile(user.uid),
    )

    # After the model answered. A failed request leaves the stored conversation
    # exactly as it was, so a retry does not replay a question twice.
    #
    # The chart itself is not stored. Forty turns of inline screenshots would
    # pass a Firestore document's one-megabyte ceiling in a handful of
    # messages, and the whole conversation would stop loading rather than one
    # image going missing. What is kept is that a chart was there, so the
    # thread still reads correctly on the next visit.
    question = payload.message
    if payload.image:
        question = f"{question} [chart attached]".strip()

    conversations_repo.store_exchange(
        user.uid, question=question, answer=completion.text
    )

    return CoachReply(
        reply=completion.text, model=completion.model, trade_count=len(trades)
    )


@router.get(
    "/conversation",
    response_model=CoachConversation,
    summary="The stored conversation",
)
def conversation(user: ReadUser) -> CoachConversation:
    """What was said last time, so a refresh resumes instead of starting over."""
    return CoachConversation(turns=conversations_repo.load_turns(user.uid))


@router.delete(
    "/conversation",
    response_model=CoachConversation,
    summary="Start the conversation over",
)
def clear_conversation(user: WriteUser) -> CoachConversation:
    conversations_repo.clear(user.uid)
    return CoachConversation(turns=[])
