"""Trade payloads.

These mirror ``StoredTrade`` in the front end. Validation is strict on
purpose: ``extra="forbid"`` means a client cannot smuggle a field the API does
not know about into a Firestore document, which is the usual way a write
endpoint grows an unintended surface.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Direction = Literal["Long", "Short"]
Compliance = Literal["yes", "no", ""]

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

    @field_validator("mistakes")
    @classmethod
    def _clean_mistakes(cls, tags: list[str]) -> list[str]:
        cleaned = [tag.strip()[:60] for tag in tags if tag.strip()]
        # Order-preserving de-duplication: a repeated tag is noise, not data.
        return list(dict.fromkeys(cleaned))

    @field_validator("screenshot")
    @classmethod
    def _safe_url(cls, value: str) -> str:
        """Only http(s). A ``javascript:`` or ``data:`` URL stored here would
        be rendered by the client as a link someone might click."""
        if value and not value.startswith(("http://", "https://")):
            raise ValueError("Must be an http or https URL.")
        return value

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
