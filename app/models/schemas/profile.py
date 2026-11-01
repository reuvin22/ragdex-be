"""Account record payloads."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

AccountType = Literal["student", "coach", "individual"]
#: "free" is the default tier: the journal and the figures computed from it.
PlanId = Literal["free", "individual", "coach"]

#: What they trade. Broad on purpose — the coach uses it for vocabulary and
#: for what a normal hold time looks like, not for anything it calculates.
MarketType = Literal[
    "forex", "crypto", "futures", "stocks", "options", "indices", "mixed"
]

#: Whose money is at risk. A funded account has rules someone else wrote and a
#: drawdown that ends the account rather than denting it, which changes what
#: good advice looks like.
FundingType = Literal["personal", "prop_firm", "demo"]

#: The three spans the dashboard reads over. Shared, so the two cards offer the
#: same choices and one can be excluded from the other.
Period = Literal["daily", "weekly", "monthly"]

#: How often the behavioural reading is re-analysed.
#:
#: A cadence rather than "every time you look", which is what it used to be —
#: every dashboard load spent a model call to re-derive a habit that changes
#: over weeks. It also decides what the finding means: a daily read is about
#: yesterday's session, a monthly one is about a pattern.
LeakCadence = Period

#: A challenge bought outright, a reset bought to retry one, or an account the
#: firm funded after a pass. Kept apart because they cost and mean different
#: things: evals and resets are money spent, funded accounts are capital won.
PropAccountKind = Literal["eval", "reset", "funded"]
PropAccountStatus = Literal["active", "blown"]
#: What the firm funds. Decides which account sizes the form offers.
PropMarket = Literal["futures", "cfd"]

#: The firms a trader lists as theirs, offered when they record an account.
MAX_PROP_FIRMS = 30

#: Enough for a busy year of challenges; past that it is a typo loop.
MAX_PROP_ACCOUNTS = 300


class PropAccount(BaseModel):
    """One prop firm account the trader has paid for or been funded with."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    kind: PropAccountKind
    #: None on rows recorded before the market was asked for.
    market: PropMarket | None = None
    firm: str = Field(min_length=1, max_length=80)
    size: Decimal = Field(ge=0, le=1_000_000_000)
    #: What it cost in USD. None on a funded account: the firm paid, not you.
    price: Decimal | None = Field(default=None, ge=0, le=1_000_000)
    status: PropAccountStatus = "active"

    @model_validator(mode="after")
    def _funded_has_no_price(self) -> PropAccount:
        if self.kind == "funded":
            self.price = None
        return self


def _clean_firms(names: list[str] | None) -> list[str] | None:
    """Trimmed, blanks dropped, and one entry per firm whatever its casing —
    "topstep" and "Topstep" are the same firm listed twice."""
    if names is None:
        return None
    kept: dict[str, str] = {}
    for name in names:
        cleaned = name.strip()
        if cleaned and cleaned.lower() not in kept:
            kept[cleaned.lower()] = cleaned[:80]
    return list(kept.values())


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
    edge_window: Period | None = None
    # The whole list, replaced on every save: it is edited a row at a time on
    # the profile page, and a full write cannot leave two rows half-merged.
    prop_accounts: list[PropAccount] | None = Field(
        default=None, max_length=MAX_PROP_ACCOUNTS
    )
    prop_firms: list[str] | None = Field(default=None, max_length=MAX_PROP_FIRMS)

    @field_validator("prop_firms")
    @classmethod
    def _unique_firms(cls, names: list[str] | None) -> list[str] | None:
        return _clean_firms(names)


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
    # How far back "what is working" looks. A different span from the
    # behavioural reading on purpose: two cards over the same window tell you
    # the same thing twice.
    edge_window: Period = "monthly"
    prop_accounts: list[PropAccount] = Field(default_factory=list)
    prop_firms: list[str] = Field(default_factory=list)
    plan: PlanId = "free"
    plan_since: datetime | None = None
    created_at: datetime | None = None
    last_seen_at: datetime | None = None


class PlanChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan: PlanId
