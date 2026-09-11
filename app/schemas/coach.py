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
HISTORY_LIMIT = 20


class CoachTurn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    role: Role
    text: str = Field(min_length=1, max_length=MAX_MESSAGE)


class CoachRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    message: str = Field(min_length=1, max_length=MAX_MESSAGE)
    language: str = Field(default="English", max_length=40)
    # Conversation shape is taken from the client; every *fact* the coach uses
    # is read from Firestore on the server, so a forged history cannot invent
    # trades that were never logged.
    history: list[CoachTurn] = Field(default_factory=list, max_length=HISTORY_LIMIT)


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
