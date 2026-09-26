"""The arena: the ladder, the boards, and the tournaments.

This family publishes one account's standing to every other account, which is
a first for this service — so it is worth being exact about what a standing
contains. A leaderboard row is a uid, a display name, a photo and a number of
points. There is no return, no balance, no P&L and no trade count anywhere in
these responses, and the scoring behind the points (``services/scoring.py``)
never reads a figure as a quantity. A rank says somebody is good; it does not
say what they are worth.

Two invariants carry the rest:

- **Nothing here scores anybody but the caller.** Points are computed from
  your own journal when you open the arena and stored; the boards read stored
  numbers. No request reads a second account's trades.
- **Nobody is ranked without entering.** The collection holds entrants only,
  and leaving deletes the row rather than hiding it.
"""

from __future__ import annotations

from fastapi import APIRouter, Path, Response, status

from app.controllers.deps import ReadUser, StandardRateLimit, WriteUser
from app.core.errors import AppError
from app.models.repositories import arena as arena_repo
from app.models.repositories import tournaments as tournaments_repo
from app.models.schemas.common import ErrorResponse, Message
from app.models.schemas.competition import (
    Battle,
    Leaderboard,
    MyArena,
    TournamentList,
    UniversityBoard,
)
from app.services import competition as service

router = APIRouter(
    prefix="/competition",
    tags=["competition"],
    dependencies=[StandardRateLimit],
    responses={
        401: {"model": ErrorResponse, "description": "Not signed in"},
        429: {"model": ErrorResponse, "description": "Rate limited"},
    },
)

TournamentId = Path(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")


@router.get("/me", response_model=MyArena, summary="Your rank, and how you earned it")
async def me(user: ReadUser) -> MyArena:
    """Rescores the caller from their own journal on the way past.

    A read with a write behind it, which is unusual enough to justify: this is
    the only moment the service is holding the one journal it is allowed to
    score, and doing it here is what lets every board avoid reading anybody
    else's.
    """
    return service.mine(user.uid)


@router.post(
    "/enter",
    response_model=Message,
    status_code=status.HTTP_201_CREATED,
    summary="Enter the ladder",
)
async def enter(user: WriteUser) -> Message:
    """Nobody is ranked without this. Idempotent."""
    arena_repo.enter(user.uid)
    service.refresh(user.uid)
    return Message(message="You are on the ladder.")


@router.delete(
    "/enter",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Leave the ladder",
)
async def leave(user: WriteUser) -> Response:
    """Off the board immediately — the row is deleted, not flagged."""
    arena_repo.leave(user.uid)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/leaderboard", response_model=Leaderboard, summary="The ladder")
async def leaderboard(user: ReadUser) -> Leaderboard:
    return service.leaderboard(user.uid)


@router.get(
    "/universities",
    response_model=UniversityBoard,
    summary="University against university",
)
async def universities(user: ReadUser) -> UniversityBoard:
    return service.universities(user.uid)


@router.get("/tournaments", response_model=TournamentList, summary="Tournaments")
async def tournaments(user: ReadUser) -> TournamentList:
    return service.tournaments(user.uid)


@router.post(
    "/tournaments/{tournament_id}/enter",
    response_model=Message,
    summary="Enter a tournament",
    responses={
        400: {"model": ErrorResponse, "description": "Entries have closed"},
        404: {"model": ErrorResponse},
    },
)
async def enter_tournament(
    user: WriteUser, tournament_id: str = TournamentId
) -> Message:
    """Only while it is open.

    A tournament scores the points earned inside its window, so letting
    somebody in half way through would hand them a shorter window and the
    same board — which is either an advantage or a penalty depending on the
    week, and neither is a competition.
    """
    tournament = tournaments_repo.get(tournament_id)

    if tournament.state != "open":
        raise AppError(
            "Entries for that tournament have closed.", code="entries_closed"
        )

    tournaments_repo.enter(tournament_id, user.uid)
    return Message(message=f"You are entered in {tournament.name}.")


@router.delete(
    "/tournaments/{tournament_id}/enter",
    response_model=Message,
    summary="Withdraw from a tournament",
    responses={404: {"model": ErrorResponse}},
)
async def withdraw(user: WriteUser, tournament_id: str = TournamentId) -> Message:
    tournaments_repo.get(tournament_id)
    tournaments_repo.withdraw(tournament_id, user.uid)
    return Message(message="Withdrawn.")


# --------------------------------------------------------------- battles


@router.get("/battle", response_model=Battle, summary="Your current match")
async def battle(user: ReadUser) -> Battle:
    """Answers from the caller's side only.

    There is no field here for what the opponent scored until the match has
    settled, because until then it does not exist anywhere either — each side
    computes its own result from its own journal and reports it. That is what
    keeps a head-to-head from being a reason for one trader's request to read
    another's trades.

    Settles the match on the way past when it can be. This service has no
    scheduler, so the thing that ends a match is somebody looking at it.
    """
    return service.battle(user.uid)


@router.post(
    "/battle/search",
    response_model=Battle,
    summary="Find an opponent",
    responses={400: {"model": ErrorResponse, "description": "Not on the ladder"}},
)
async def search(user: WriteUser) -> Battle:
    """Claim a waiting opponent, or join the queue.

    The pairing runs in a Firestore transaction: two traders pressing this in
    the same second would otherwise both read the same waiting opponent and
    both claim them, leaving one of them in a match the other knows nothing
    about.
    """
    return service.search(user.uid)


@router.delete(
    "/battle/search",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Stop looking",
)
async def cancel_search(user: WriteUser) -> Response:
    service.cancel_search(user.uid)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
