"""Journal CRUD.

Note what no route accepts: a uid. It comes from the verified token on every
call, so there is no path — not a query string, not a body, not a header — by
which a caller can address another trader's journal.
"""

from __future__ import annotations

from fastapi import APIRouter, Path, Query, Response, status

from app.api.deps import ReadUser, StandardRateLimit, WriteUser
from app.repositories import trades as repo
from app.schemas.common import ErrorResponse
from app.schemas.trade import Trade, TradeCreate, TradePage, TradeUpdate

router = APIRouter(
    prefix="/trades",
    tags=["trades"],
    dependencies=[StandardRateLimit],
    responses={
        401: {"model": ErrorResponse, "description": "Not signed in"},
        429: {"model": ErrorResponse, "description": "Rate limited"},
    },
)

# Firestore ids are 20 alphanumerics. Constraining the path keeps anything
# stranger from reaching the client library at all.
TradeId = Path(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")


@router.get("", response_model=TradePage, summary="List journal entries")
async def list_trades(
    user: ReadUser,
    limit: int = Query(default=50, ge=1, le=repo.MAX_PAGE_SIZE),
    cursor: str | None = Query(default=None, max_length=256),
) -> TradePage:
    """Newest first, cursor-paged."""
    items, next_cursor = repo.list_trades(user.uid, limit=limit, cursor=cursor)
    return TradePage(items=items, next_cursor=next_cursor)


@router.get(
    "/{trade_id}",
    response_model=Trade,
    summary="Read one trade",
    responses={404: {"model": ErrorResponse}},
)
async def get_trade(user: ReadUser, trade_id: str = TradeId) -> Trade:
    return repo.get_trade(user.uid, trade_id)


@router.post(
    "",
    response_model=Trade,
    status_code=status.HTTP_201_CREATED,
    summary="Log a trade",
    responses={403: {"model": ErrorResponse, "description": "Email not confirmed"}},
)
async def create_trade(user: WriteUser, payload: TradeCreate) -> Trade:
    """P&L and R:R are computed server-side and ignored if sent."""
    return repo.create_trade(user.uid, payload)


@router.patch(
    "/{trade_id}",
    response_model=Trade,
    summary="Edit a trade",
    responses={404: {"model": ErrorResponse}},
)
async def update_trade(
    user: WriteUser, payload: TradeUpdate, trade_id: str = TradeId
) -> Trade:
    return repo.update_trade(user.uid, trade_id, payload)


@router.delete(
    "/{trade_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a trade",
    responses={404: {"model": ErrorResponse}},
)
async def delete_trade(user: WriteUser, trade_id: str = TradeId) -> Response:
    repo.delete_trade(user.uid, trade_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
