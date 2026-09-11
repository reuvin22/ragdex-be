"""Account record payloads."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

AccountType = Literal["student", "coach", "individual"]
PlanId = Literal["individual", "coach"]


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
    plan: PlanId = "individual"
    plan_since: datetime | None = None
    created_at: datetime | None = None
    last_seen_at: datetime | None = None


class PlanChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan: PlanId
