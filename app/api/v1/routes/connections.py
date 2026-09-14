"""Connecting a broker account.

The most sensitive route in the service: it is the one a trader hands a broker
credential to. Three things it does differently as a result.

**It asks for the investor password and says so.** Read-only at the broker —
it cannot place an order or move money. The client says this on the form too;
the wording matters more than the encryption, because a trader who understands
what they are handing over is one who can decide whether to.

**It never gives a credential back.** The response model has no field for one,
so no careless return can leak it. Editing credentials is not supported either:
delete and reconnect.

**It is rate limited hard.** Every connection costs real money per month in
bridge fees, so creating them is the one action in this app where a loop is a
billing incident rather than a slow afternoon.
"""

from __future__ import annotations

from fastapi import APIRouter, status

from app.api.deps import AppSettings, ReadUser, WriteUser
from app.core.errors import AppError, NotFoundError
from app.repositories import connections as repo
from app.schemas.common import ErrorResponse
from app.schemas.connection import (
    Connection,
    ConnectionCreate,
    ConnectionList,
    ConnectionUpdate,
    SyncReport,
)
from app.services.bridges import bridge_for
from app.services.broker_sync import sync_connection

router = APIRouter(
    prefix="/connections",
    tags=["connections"],
    responses={
        401: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
    },
)


@router.get("", response_model=ConnectionList, summary="Connected broker accounts")
def list_connections(user: ReadUser) -> ConnectionList:
    return ConnectionList(items=repo.list_connections(user.uid))


@router.post(
    "",
    response_model=Connection,
    status_code=status.HTTP_201_CREATED,
    summary="Connect a broker account",
    responses={409: {"model": ErrorResponse, "description": "Too many accounts"}},
)
def create_connection(user: WriteUser, payload: ConnectionCreate) -> Connection:
    """Store the credentials and report the account as pending.

    Pending, not connected: standing an account up at the bridge takes up to a
    minute and can simply fail. The client polls this list, so the honest
    answer now is better than an optimistic one that has to be taken back.
    """
    try:
        return repo.create_connection(user.uid, payload)
    except repo.ConnectionLimit:
        raise AppError(
            f"You can connect up to {repo.MAX_CONNECTIONS} accounts. "
            "Disconnect one to add another.",
            status_code=409,
            code="too_many_connections",
        ) from None


@router.patch(
    "/{connection_id}", response_model=Connection, summary="Rename or pause an account"
)
def update_connection(
    user: WriteUser, connection_id: str, payload: ConnectionUpdate
) -> Connection:
    updated = repo.update_connection(user.uid, connection_id, payload)
    if updated is None:
        raise NotFoundError("That connection does not exist.")
    return updated


@router.delete(
    "/{connection_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Disconnect an account",
)
def delete_connection(user: WriteUser, connection_id: str) -> None:
    """Forget the account and its credentials.

    Trades already imported stay. They are the trader's record of their own
    trading, not the connection's property, and disconnecting a broker should
    not silently delete a year of journal.
    """
    if not repo.delete_connection(user.uid, connection_id):
        raise NotFoundError("That connection does not exist.")


@router.post(
    "/{connection_id}/sync",
    response_model=SyncReport,
    summary="Import new trades from this account",
    responses={
        501: {"model": ErrorResponse, "description": "Broker sync not configured"},
        502: {"model": ErrorResponse, "description": "The bridge could not answer"},
    },
)
def sync_now(user: WriteUser, connection_id: str, settings: AppSettings) -> SyncReport:
    """Pull whatever has closed since the last pass.

    Exposed as a route as well as being run on a schedule, for three reasons
    that all come down to the same thing — a trader should never be stuck
    waiting on a timer they cannot see:

    * the first sync after connecting, which is the one they are watching;
    * "I closed a trade two minutes ago and it is not here yet";
    * a connection that failed on a typo, retried after fixing it.

    Idempotent. Running it twice imports nothing the second time, so a trader
    leaning on the button costs a round trip and nothing else.
    """
    result = sync_connection(user.uid, connection_id, bridge_for(settings))

    return SyncReport(
        added=result.added,
        seen=result.seen,
        synced_through=result.through,
    )
