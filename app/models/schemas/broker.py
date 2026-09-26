"""Broker connection payloads."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr

#: Brokers this app can connect. One, for now — the shape is here so adding a
#: second is a new value rather than a new schema.
BrokerId = Literal["tradovate"]

#: Which set of books the connection reaches. Simulation only, deliberately:
#: Tradovate has no read-only credential, so a live token would be one that
#: could move real money. See services/tradovate.py.
BrokerEnvironment = Literal["demo"]


class TradovateConnect(BaseModel):
    """A trader's own Tradovate login, sent once.

    ``SecretStr`` so the password cannot end up in a log line, a traceback or
    a ``repr`` by accident — pydantic prints it as ``**********`` wherever a
    model is dumped, and reading it takes a deliberate call.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    username: str = Field(min_length=1, max_length=120)
    password: SecretStr = Field(min_length=1, max_length=200)


class BrokerAccount(BaseModel):
    """One account behind the connection, as something to recognise."""

    id: int
    name: str
    nickname: str = ""


class BrokerConnection(BaseModel):
    """What the client is told about the connection.

    Note what is absent: no access token, no market-data token, no password.
    The client never needs one, and a field that is never sent is a field that
    cannot leak through a screenshot, a cache or a browser extension.
    """

    connected: bool
    broker: BrokerId | None = None
    environment: BrokerEnvironment | None = None
    #: The Tradovate username this is connected as, so the trader can see
    #: which login is in use without going and checking.
    username: str = ""
    accounts: list[BrokerAccount] = Field(default_factory=list)
    connected_at: datetime | None = None
    #: When the stored token stops working. The client shows this rather than
    #: pretending a connection is permanent.
    expires_at: datetime | None = None
