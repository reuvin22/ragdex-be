"""Connecting a trading account.

Tradovate, against simulation only. The reasoning for that restriction is in
``services/tradovate.py`` and it is not a setting: Tradovate has no read-only
credential, so a live connection would mean this service holding something
that can place orders with real money.

The shape of every route here follows the same rule as the rest of the API:
identity comes from the session cookie, never from the request. There is no
uid in a path or a body, so a caller can only ever connect, read or disconnect
their own account.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, status

from app.controllers.deps import AppSettings, ReadUser, StandardRateLimit, WriteUser
from app.core.errors import AppError
from app.models.repositories import brokers as repo
from app.models.schemas.broker import BrokerConnection, TradovateConnect
from app.models.schemas.common import ErrorResponse, Message
from app.services import tradovate

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/brokers",
    tags=["brokers"],
    responses={
        401: {"model": ErrorResponse, "description": "Not signed in"},
        501: {"model": ErrorResponse, "description": "Tradovate not configured"},
    },
)


@router.get("", response_model=BrokerConnection, summary="The connected account")
def read_connection(user: ReadUser) -> BrokerConnection:
    """Answers a not-connected record rather than 404.

    The client asks this on every visit to Settings to choose between the
    connect form and the connected panel, so "nothing connected" is an
    ordinary answer here and not an error.
    """
    return repo.get(user.uid)


@router.post(
    "/tradovate",
    response_model=BrokerConnection,
    status_code=status.HTTP_201_CREATED,
    summary="Connect a Tradovate simulation account",
    dependencies=[StandardRateLimit],
)
async def connect_tradovate(
    user: WriteUser, payload: TradovateConnect, settings: AppSettings
) -> BrokerConnection:
    """Exchange a Tradovate login for a stored token.

    The password lives for the length of this function. It goes into one
    outbound request and is never written to Firestore, never logged, and
    never returned — reconnecting later means typing it again, which is the
    correct trade for not holding it.

    The account list is fetched before anything is stored. It doubles as a
    check that the token actually works, and it gives the trader a name they
    recognise instead of the bare word "connected".
    """
    credential = await tradovate.sign_in(
        payload.username,
        payload.password.get_secret_value(),
        settings,
    )

    accounts = await tradovate.accounts(credential.access_token, settings)

    repo.save(
        user.uid,
        broker="tradovate",
        environment=tradovate.ENVIRONMENT,
        username=credential.username,
        tradovate_user_id=credential.user_id,
        access_token=credential.access_token,
        md_access_token=credential.md_access_token,
        expires_at=credential.expires_at,
        accounts=accounts,
    )

    # The uid, not the Tradovate username: this line exists to answer "did
    # this account connect", and putting somebody's broker login in a log is
    # the sort of thing this whole module is arranged to avoid.
    logger.info("Connected a Tradovate simulation account for %s", user.uid)

    return repo.get(user.uid)


@router.delete("/tradovate", response_model=Message, summary="Disconnect")
def disconnect_tradovate(user: WriteUser) -> Message:
    """Drop the stored credential.

    Deliberately does not care whether one was there. Disconnecting is a
    statement about the end state, and answering 404 to somebody trying to
    make sure nothing is stored would be an unhelpful way to agree with them.
    """
    repo.delete(user.uid)
    return Message(message="Tradovate disconnected.")


@router.get(
    "/tradovate/accounts",
    response_model=BrokerConnection,
    summary="Re-read the accounts behind the connection",
    dependencies=[StandardRateLimit],
)
async def refresh_accounts(user: ReadUser, settings: AppSettings) -> BrokerConnection:
    """Ask Tradovate again, and notice when the token has stopped working.

    A stored token expires, and nothing else here would find that out until
    the trader wondered why an import was empty. This is the call that turns
    a dead credential into a visible "connect it again" rather than a silent
    one.
    """
    token = repo.access_token(user.uid)
    if token is None:
        raise AppError(
            "No Tradovate account is connected.",
            status_code=status.HTTP_404_NOT_FOUND,
            code="not_connected",
        )

    accounts = await tradovate.accounts(token, settings)

    connection = repo.get(user.uid)
    return connection.model_copy(update={"accounts": accounts})
