"""Broker account connections.

What the trader gives us to read their account, and what comes back out — which
is deliberately not the same thing. Nothing here ever returns a credential.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: Which terminal the account lives on. The two speak different protocols and
#: report history differently, so the bridge has to be told which it is.
Platform = Literal["mt4", "mt5"]

#: Where a connection is in its life. `pending` is the window between the
#: trader submitting credentials and the bridge confirming they work — it can
#: take a minute, and a UI that showed "connected" immediately would be lying.
ConnectionState = Literal["pending", "connected", "failed", "disconnected"]


class ConnectionCreate(BaseModel):
    """What a trader submits to connect an account.

    The password asked for is the **investor** password, and that is a product
    decision as much as a security one: it is read-only at the broker, so it
    cannot place a trade or move money even if this database were opened. A
    journal has no business holding anything stronger, and saying so plainly on
    the form is what makes handing it over reasonable.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    platform: Platform
    #: The broker's server name, exactly as the terminal shows it —
    #: "ICMarketsSC-MT5", "FTMO-Server". Brokers are inconsistent about these
    #: and a near miss fails to connect, so it is taken verbatim.
    server: str = Field(min_length=1, max_length=120)
    #: The account number. A string, not an int: some brokers zero-pad.
    login: str = Field(min_length=1, max_length=40)
    investor_password: str = Field(min_length=1, max_length=200)
    #: What the trader calls this account. Their own words, since "FTMO 100k
    #: Phase 2" means something to them and the login number does not.
    label: str = Field(default="", max_length=60)

    @field_validator("login")
    @classmethod
    def _digits_ish(cls, value: str) -> str:
        # Not strictly numeric — a few brokers use alphanumeric logins — but a
        # login with spaces or punctuation in it is a typo, and failing here
        # is faster than failing at the broker two minutes later.
        if not value.replace("-", "").replace("_", "").isalnum():
            raise ValueError("That does not look like an account number.")
        return value


class ConnectionUpdate(BaseModel):
    """Only the things a trader can change without reconnecting."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    label: str | None = Field(default=None, max_length=60)
    #: Pausing a connection stops the sync without forgetting the account.
    paused: bool | None = None


class Connection(BaseModel):
    """A connection as the app sees it.

    Note what is absent: no password, no bridge account id, no token. This
    model is the reason those cannot leak — there is no field for them to leak
    into, so a careless `return record` cannot expose one.
    """

    id: str
    platform: Platform
    server: str
    #: Shown back so the trader can tell two accounts apart. Not a secret; it
    #: is printed on their broker's dashboard and in the terminal title bar.
    login: str
    label: str
    state: ConnectionState
    paused: bool
    #: Why it failed, in words a trader can act on. Empty unless state failed.
    message: str = ""
    created_at: datetime | None = None
    last_synced_at: datetime | None = None
    #: How many trades have come in through this connection, ever.
    trades_synced: int = 0


class ConnectionList(BaseModel):
    items: list[Connection]
