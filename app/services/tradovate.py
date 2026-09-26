"""Tradovate, reached from the server.

The trader gives us their Tradovate username and password once. This module
exchanges them for an access token and **returns the token, never the
password** — nothing above this line ever sees the password again and nothing
anywhere writes it down.

**Simulation only, and that is a security control rather than a convenience.**

Tradovate has no read-only credential. MetaTrader has an investor password
that can see an account but cannot trade it, and the old broker screen in this
app leaned on exactly that to justify asking. Tradovate offers no equivalent:
an access token is the whole account. So the host below is the demo one,
written as a constant with no switch, no setting and no parameter. A token
minted here reaches simulated funds and nothing else.

Turning this into a live integration is therefore not a matter of changing a
string. It needs a decision about holding a credential that can move real
money, and probably a different answer than "store it" — which is why there is
nothing here to flip.

Nothing this module returns reaches a client verbatim. Tradovate answers
failures with an ``errorText`` written for a developer, and its rate limiting
answers with a penalty ticket rather than an error at all; both are translated
here into something a person can act on.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi import status

from app.core.config import Settings
from app.core.errors import AppError, UpstreamError

logger = logging.getLogger(__name__)

#: The simulation host. See the module docstring: this is deliberately not
#: configurable, and the live host must not appear anywhere in this package —
#: a grep for it should return nothing, which is why it is not written out
#: even as an example.
_BASE = "https://demo.tradovateapi.com/v1"

#: What we call this environment everywhere else — stored on the connection
#: and shown in the UI, so a record can never be mistaken for a live one.
ENVIRONMENT = "demo"

#: Tradovate answers a throttled auth request with a ticket and a number of
#: seconds rather than an error. Waiting is the documented way through it, but
#: waiting inside a request is not: past this many seconds we give up and tell
#: the trader to try again shortly, rather than holding a worker open.
_MAX_PENALTY_SECONDS = 10


class TradovateError(AppError):
    """Something the trader can act on: wrong password, locked account."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "broker_rejected",
        status_code: int = status.HTTP_400_BAD_REQUEST,
    ) -> None:
        super().__init__(message, status_code=status_code, code=code)


@dataclass(slots=True)
class Credential:
    """What a successful sign-in hands back. The password is not in here."""

    access_token: str
    #: The market-data token. Stored because it is issued with the same call
    #: and cannot be re-requested on its own.
    md_access_token: str
    expires_at: datetime
    user_id: int
    username: str


@dataclass(slots=True, frozen=True)
class Account:
    """One trading account behind the credential."""

    id: int
    name: str
    nickname: str


def _timeout(settings: Settings) -> httpx.Timeout:
    return httpx.Timeout(settings.tradovate_timeout_seconds, connect=10.0)


def _expiry(raw: Any) -> datetime:
    """Tradovate's ISO expiry, as an aware datetime.

    Falls back to "already expired" rather than to a generous default: a
    connection wrongly believed fresh fails later at the broker, where the
    error is opaque. One believed stale is simply renewed.
    """
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        logger.warning("Tradovate sent an expiry that could not be parsed")
        return datetime.now(UTC)

    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


async def _post(
    client: httpx.AsyncClient, path: str, payload: dict[str, Any]
) -> dict[str, Any]:
    try:
        response = await client.post(f"{_BASE}{path}", json=payload)
    except httpx.HTTPError as exc:
        logger.warning("Tradovate unreachable: %s", type(exc).__name__)
        raise UpstreamError("Could not reach Tradovate right now.") from exc

    if response.status_code == status.HTTP_401_UNAUTHORIZED:
        raise TradovateError(
            "Tradovate rejected those details.",
            code="broker_credentials",
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    if not response.is_success:
        logger.warning("Tradovate %s answered %s", path, response.status_code)
        raise UpstreamError("Tradovate could not be reached right now.")

    body: dict[str, Any] = response.json() if response.content else {}
    return body


async def sign_in(username: str, password: str, settings: Settings) -> Credential:
    """Exchange a trader's Tradovate login for an access token.

    The password appears in exactly one place — the body of this request — and
    is not logged, returned or persisted. If this call fails, nothing about the
    attempt is written down either.
    """
    if not settings.tradovate_configured:
        raise AppError(
            "Tradovate is not configured on the server.",
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            code="not_configured",
        )

    # Re-read rather than asserting: `tradovate_configured` already proved
    # this, but an assert is stripped under -O and this is the value that
    # authenticates the whole application.
    secret = settings.tradovate_sec
    if secret is None:  # pragma: no cover — unreachable via the check above
        raise AppError(
            "Tradovate is not configured on the server.",
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            code="not_configured",
        )

    payload: dict[str, Any] = {
        "name": username,
        "password": password,
        "appId": settings.tradovate_app_id,
        "appVersion": settings.tradovate_app_version,
        "cid": settings.tradovate_cid,
        "sec": secret.get_secret_value(),
    }

    async with httpx.AsyncClient(timeout=_timeout(settings)) as client:
        body = await _post(client, "/auth/accessTokenRequest", payload)
        body = await _past_penalty(client, payload, body)

    return _credential(body, username)


async def _past_penalty(
    client: httpx.AsyncClient, payload: dict[str, Any], body: dict[str, Any]
) -> dict[str, Any]:
    """Wait out a throttle, once, or give up with something readable.

    Tradovate answers a throttled request with a 200 carrying a ticket and a
    delay rather than an error status. Retrying immediately simply earns
    another one, so the documented way through is to wait the stated time and
    present the ticket.

    Only a short wait, and only one retry. A request holding a worker open for
    a minute to serve one person is worse than asking them to press the button
    again, and the ticket outlives this request anyway.

    A captcha ends it: that is Tradovate deciding it wants a human, and
    answering it from a server is precisely what it exists to stop.
    """
    ticket = body.get("p-ticket")
    if not ticket:
        return body

    if body.get("p-captcha"):
        logger.warning("Tradovate demanded a captcha for a sign-in")
        raise TradovateError(
            "Tradovate wants to check this sign-in directly. Log in on "
            "Tradovate once, then connect again.",
            code="broker_captcha",
        )

    try:
        wait = float(body.get("p-time") or 0)
    except (TypeError, ValueError):
        wait = 0.0

    if wait > _MAX_PENALTY_SECONDS:
        logger.info("Tradovate penalty of %ss is too long to wait out", wait)
        raise TradovateError(
            "Tradovate is rate limiting sign-ins. Try again in a minute.",
            code="broker_throttled",
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        )

    await asyncio.sleep(max(0.0, wait))
    return await _post(
        client, "/auth/accessTokenRequest", {**payload, "p-ticket": ticket}
    )


def _credential(body: dict[str, Any], username: str) -> Credential:
    token = str(body.get("accessToken", ""))
    if not token:
        # Tradovate reports a bad password as a 200 with errorText, not a 401,
        # so this is the branch that actually catches a wrong login.
        reason = str(body.get("errorText", "")).strip()
        logger.info("Tradovate declined a sign-in: %s", reason or "no reason given")
        raise TradovateError(
            "Tradovate did not accept that username and password.",
            code="broker_credentials",
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    return Credential(
        access_token=token,
        md_access_token=str(body.get("mdAccessToken", "")),
        expires_at=_expiry(body.get("expirationTime")),
        user_id=int(body.get("userId") or 0),
        username=str(body.get("name") or username),
    )


async def accounts(access_token: str, settings: Settings) -> list[Account]:
    """The accounts the credential can see.

    Fetched at connect time so the trader is shown something recognisable —
    an account name they know — as proof the connection is real, rather than
    the word "connected" and nothing to check it against.
    """
    try:
        async with httpx.AsyncClient(timeout=_timeout(settings)) as client:
            response = await client.get(
                f"{_BASE}/account/list",
                headers={"Authorization": f"Bearer {access_token}"},
            )
    except httpx.HTTPError as exc:
        logger.warning("Tradovate account list unreachable: %s", type(exc).__name__)
        raise UpstreamError("Could not reach Tradovate right now.") from exc

    if response.status_code == status.HTTP_401_UNAUTHORIZED:
        raise TradovateError(
            "That Tradovate connection has expired. Connect it again.",
            code="broker_expired",
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    if not response.is_success:
        raise UpstreamError("Tradovate could not be reached right now.")

    raw = response.json() if response.content else []
    if not isinstance(raw, list):
        return []

    return [
        Account(
            id=int(entry.get("id") or 0),
            name=str(entry.get("name") or ""),
            nickname=str(entry.get("nickname") or ""),
        )
        for entry in raw
        if isinstance(entry, dict)
    ]
