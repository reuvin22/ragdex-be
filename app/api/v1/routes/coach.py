"""AI coach conversation.

The journal is read here with the Admin SDK and never accepted from the
client, so the coach can only ever talk about trades the trader actually
logged. A forged history changes the shape of the conversation, not the facts
in it.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import AppSettings, CoachRateLimit, WriteUser
from app.repositories import trades as trades_repo
from app.schemas.coach import CoachReply, CoachRequest
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

    completion = await coach_service.ask(
        history=payload.history,
        message=payload.message,
        summary=summary,
        language=payload.language,
        display_name=user.name or "",
        settings=settings,
    )

    return CoachReply(
        reply=completion.text, model=completion.model, trade_count=len(trades)
    )
