"""AI coach payloads."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Role = Literal["user", "coach"]
# Named so the service that narrows a model's answer to it can say so.
Severity = Literal["low", "medium", "high"]

# Matches the front end's limits, so a message the UI accepts is never
# rejected here and vice versa.
MAX_MESSAGE = 2_000

# What the coach itself may have said. Larger than a trader's message because
# the coach now answers at length — advice, what it buys them, and what to stop
# doing — and a stored turn has to survive being validated on the way back out.
MAX_REPLY = 8_000

#: Turns replayed to the model on any one request. The store keeps more than
#: this for the trader to scroll; only the newest reach the prompt.
HISTORY_LIMIT = 20


class CoachTurn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    role: Role
    text: str = Field(min_length=1, max_length=MAX_REPLY)


class CoachRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    message: str = Field(min_length=1, max_length=MAX_MESSAGE)
    language: str = Field(default="English", max_length=40)
    # No history field. The conversation is read from the server's own store,
    # so the client no longer supplies the one part of a coach request it could
    # previously make up — and a refresh no longer erases what was said.


class CoachConversation(BaseModel):
    """The stored conversation, oldest turn first."""

    turns: list[CoachTurn]


class CoachReply(BaseModel):
    reply: str
    model: str
    trade_count: int


class LeakResult(BaseModel):
    title: str
    finding: str
    cost_label: str = ""
    severity: Severity = "low"
    recommendation: str = ""


class LeakResponse(BaseModel):
    result: LeakResult | None = None
    trade_count: int
    needed: int | None = None
