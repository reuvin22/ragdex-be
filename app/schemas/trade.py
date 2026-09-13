"""Trade payloads.

These mirror ``StoredTrade`` in the front end. Validation is strict on
purpose: ``extra="forbid"`` means a client cannot smuggle a field the API does
not know about into a Firestore document, which is the usual way a write
endpoint grows an unintended surface.
"""

from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

#: An image in the bucket, as ``<folder>/<uid>/<random>.<ext>``.
#:
#: Deliberately narrow rather than "anything without a colon". This is the
#: one value in a trade the client renders as a source, so the shapes it may
#: hold are spelled out rather than inferred from what is *not* dangerous.
_STORAGE_KEY = re.compile(
    r"(?:profile|charts|ai|messages)/"
    r"[A-Za-z0-9_-]{1,128}/"
    r"[A-Za-z0-9_-]{1,80}\.[a-z0-9]{1,8}"
)

Direction = Literal["Long", "Short"]
Compliance = Literal["yes", "no", ""]

#: Which session a trade was taken in. An empty list means unknown — either the
#: trader did not say and there was no entry time to work it out from, or the
#: trade predates the field.
TradingSession = Literal["asia", "london", "newyork"]

#: Two, because the real sessions overlap in pairs — Asia runs into London,
#: London into New York — and a trade held through one of those handovers
#: genuinely belongs to both. It cannot belong to three: Asia and New York
#: share no hour, so a third entry is a mistake rather than a longer trade.
MAX_SESSIONS = 2

# Money and sizes are bounded so a typo cannot write an absurd document, and
# so downstream arithmetic cannot be handed an infinity.
Money = Annotated[Decimal, Field(ge=-1_000_000_000, le=1_000_000_000)]
Size = Annotated[Decimal, Field(ge=0, le=1_000_000_000)]

# Defined as functions, not shared Field instances: one FieldInfo object
# reused across several fields is a subtle way to have them share state.


def _text() -> Any:
    return Field(default="", max_length=2_000)


def _short_text() -> Any:
    return Field(default="", max_length=120)


class TradeBase(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    ticker: str = Field(min_length=1, max_length=24)
    direction: Direction
    size: Size | None = None
    size_unit: str = Field(default="Shares", max_length=24)

    entry_price: Money | None = None
    exit_price: Money | None = None
    entry_at: datetime | None = None
    exit_at: datetime | None = None

    setup: str = _short_text()
    # Left empty by a trader who does not know, and filled in from the entry
    # time by the server. See session_for() in repositories/trades.
    sessions: list[TradingSession] = Field(
        default_factory=list, max_length=MAX_SESSIONS
    )
    rationale: str = _text()
    stop_loss: Money | None = None
    take_profit: Money | None = None
    screenshot: str = Field(default="", max_length=2_000)

    complied_entry: Compliance = ""
    complied_exit: Compliance = ""
    complied_management: Compliance = ""
    emotion_before: str = _short_text()
    emotion_during: str = _short_text()
    mistakes: list[str] = Field(default_factory=list, max_length=20)
    # Anything the other fields have no box for. Last in the form and last
    # here: it is the catch-all, not a required part of logging a trade.
    notes: str = _text()

    @field_validator("mistakes")
    @classmethod
    def _clean_mistakes(cls, tags: list[str]) -> list[str]:
        cleaned = [tag.strip()[:60] for tag in tags if tag.strip()]
        # Order-preserving de-duplication: a repeated tag is noise, not data.
        return list(dict.fromkeys(cleaned))

    @field_validator("sessions")
    @classmethod
    def _unique_sessions(cls, sessions: list[str]) -> list[str]:
        # Order-preserving, like the tags above. Naming a session twice is a
        # double tap on the button, not a trade that crossed it twice.
        return list(dict.fromkeys(sessions))

    @field_validator("screenshot")
    @classmethod
    def _safe_url(cls, value: str) -> str:
        """A link the trader pasted, or the key of an image they uploaded.

        Both are allowed because the field takes either. Nothing else is: a
        ``javascript:`` or ``data:`` URL stored here would be rendered by
        the client as a link someone might click. A key carries no scheme,
        so the two shapes cannot be confused for one another.
        """
        if not value or value.startswith(("http://", "https://")):
            return value
        if _STORAGE_KEY.fullmatch(value):
            return value
        raise ValueError("Must be an http(s) URL or an uploaded image.")

    @model_validator(mode="after")
    def _exit_after_entry(self) -> TradeBase:
        if self.entry_at and self.exit_at and self.exit_at < self.entry_at:
            raise ValueError("Exit time cannot be before entry time.")
        return self


class TradeCreate(TradeBase):
    """What a client may send. Note what is absent: no id, no uid, no
    computed figures — those are the server's to decide."""


class TradeUpdate(BaseModel):
    """A partial edit. Every field optional; unset fields are left alone."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    ticker: str | None = Field(default=None, min_length=1, max_length=24)
    direction: Direction | None = None
    size: Size | None = None
    size_unit: str | None = Field(default=None, max_length=24)
    entry_price: Money | None = None
    exit_price: Money | None = None
    entry_at: datetime | None = None
    exit_at: datetime | None = None
    setup: str | None = Field(default=None, max_length=120)
    sessions: list[TradingSession] | None = Field(
        default=None, max_length=MAX_SESSIONS
    )
    rationale: str | None = Field(default=None, max_length=2_000)
    stop_loss: Money | None = None
    take_profit: Money | None = None
    screenshot: str | None = Field(default=None, max_length=2_000)
    complied_entry: Compliance | None = None
    complied_exit: Compliance | None = None
    complied_management: Compliance | None = None
    emotion_before: str | None = Field(default=None, max_length=120)
    emotion_during: str | None = Field(default=None, max_length=120)
    mistakes: list[str] | None = Field(default=None, max_length=20)
    notes: str | None = Field(default=None, max_length=2_000)

    @field_validator("sessions")
    @classmethod
    def _unique_sessions(cls, sessions: list[str] | None) -> list[str] | None:
        # TradeUpdate does not inherit TradeBase, so the same rule is stated
        # twice rather than being quietly missing on the edit path.
        return None if sessions is None else list(dict.fromkeys(sessions))


class Trade(TradeBase):
    """A stored trade, with what the server worked out."""

    id: str
    net_pl: Decimal | None = None
    risk_reward: Decimal | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class TradePage(BaseModel):
    """A page of journal entries.

    Cursor-based rather than offset: a journal is append-heavy, and an offset
    silently skips or repeats rows when something is inserted mid-read.
    """

    items: list[Trade]
    next_cursor: str | None = None
