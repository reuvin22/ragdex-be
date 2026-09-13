"""Account record payloads."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

AccountType = Literal["student", "coach", "individual"]
PlanId = Literal["individual", "coach"]

#: What they trade. Broad on purpose — the coach uses it for vocabulary and
#: for what a normal hold time looks like, not for anything it calculates.
MarketType = Literal[
    "forex", "crypto", "futures", "stocks", "options", "indices", "mixed"
]

#: Whose money is at risk. A funded account has rules someone else wrote and a
#: drawdown that ends the account rather than denting it, which changes what
#: good advice looks like.
FundingType = Literal["personal", "prop_firm", "demo"]

#: How often the behavioural leak is re-analysed.
#:
#: A cadence rather than "every time you look", which is what it used to be —
#: every dashboard load spent a model call to re-derive a habit that changes
#: over weeks. It also decides what the finding means: a daily read is about
#: yesterday's session, a monthly one is about a pattern.
LeakCadence = Literal["daily", "weekly", "monthly"]


class ProfileUpdate(BaseModel):
    """The fields a trader may edit about themselves.

    Everything identity-related — uid, email, providers, timestamps — is
    absent by design: those come from the verified token or from the server,
    never from a request body.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    display_name: str | None = Field(default=None, max_length=80)
    account_type: AccountType | None = None
    photo_url: str | None = Field(default=None, max_length=2_000)
    timezone: str | None = Field(default=None, max_length=64)
    currency: str | None = Field(default=None, max_length=8)
    opening_balance: Decimal | None = Field(default=None, ge=0, le=1_000_000_000)
    trading_style: str | None = Field(default=None, max_length=64)
    markets: list[str] | None = Field(default=None, max_length=20)
    bio: str | None = Field(default=None, max_length=1_000)
    coach_language: str | None = Field(default=None, max_length=40)

    # -- Trading setup ---------------------------------------------------
    # What the coach needs to judge execution rather than guess at it. Without
    # these it can see that a position was larger than the last one; with them
    # it can say the trade broke a 1% limit the trader set themselves.
    market_type: MarketType | None = None
    funding_type: FundingType | None = None
    # Only meaningful alongside funding_type "prop_firm".
    prop_firm: str | None = Field(default=None, max_length=80)
    account_size: Decimal | None = Field(default=None, ge=0, le=1_000_000_000)
    # Percentages of the account, which travel across account sizes in a way
    # that a cash figure does not.
    risk_per_trade_pct: Decimal | None = Field(default=None, ge=0, le=100)
    max_daily_loss_pct: Decimal | None = Field(default=None, ge=0, le=100)
    # The reward they plan per unit of risk: 2 means risking one to make two.
    target_r: Decimal | None = Field(default=None, ge=0, le=100)
    max_trades_per_day: int | None = Field(default=None, ge=0, le=200)
    strategies: list[str] | None = Field(default=None, max_length=20)
    # The non-negotiables, in their own words. Free text because a rule that
    # matters to one trader is nonsense to another, and a fixed list would
    # collect the ones we thought of rather than the ones they break.
    trading_rules: str | None = Field(default=None, max_length=2_000)
    leak_cadence: LeakCadence | None = None


class Profile(BaseModel):
    uid: str
    email: str | None = None
    display_name: str = ""
    photo_url: str = ""
    account_type: AccountType = "individual"
    timezone: str | None = None
    currency: str | None = None
    opening_balance: Decimal | None = None
    trading_style: str | None = None
    markets: list[str] = Field(default_factory=list)
    bio: str = ""
    coach_language: str | None = None
    market_type: MarketType | None = None
    funding_type: FundingType | None = None
    prop_firm: str = ""
    account_size: Decimal | None = None
    risk_per_trade_pct: Decimal | None = None
    max_daily_loss_pct: Decimal | None = None
    target_r: Decimal | None = None
    max_trades_per_day: int | None = None
    strategies: list[str] = Field(default_factory=list)
    trading_rules: str = ""
    leak_cadence: LeakCadence = "daily"
    plan: PlanId = "individual"
    plan_since: datetime | None = None
    created_at: datetime | None = None
    last_seen_at: datetime | None = None


class PlanChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan: PlanId
