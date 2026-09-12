"""Account record storage.

Same rule as the journal: the uid comes from the verified token and is the
only way in. Identity fields are written from token claims, never from the
request body, so a caller cannot rewrite whose account this is.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from google.cloud.firestore_v1 import SERVER_TIMESTAMP

from app.core.errors import NotFoundError
from app.core.security import CurrentUser
from app.db.firestore import profile_doc
from app.schemas.profile import PlanId, Profile, ProfileUpdate

from . import billing, directory

_FIELDS = {
    "display_name": "displayName",
    "account_type": "accountType",
    "photo_url": "photoURL",
    "timezone": "timezone",
    "currency": "currency",
    "opening_balance": "openingBalance",
    "trading_style": "tradingStyle",
    "markets": "markets",
    "bio": "bio",
    "coach_language": "coachLanguage",
    "market_type": "marketType",
    "funding_type": "fundingType",
    "prop_firm": "propFirm",
    "account_size": "accountSize",
    "risk_per_trade_pct": "riskPerTradePct",
    "max_daily_loss_pct": "maxDailyLossPct",
    "target_r": "targetR",
    "max_trades_per_day": "maxTradesPerDay",
    "strategies": "strategies",
    "trading_rules": "tradingRules",
}


def _decimal(value: Any) -> Decimal | None:
    """A stored number back as a Decimal, or None if it was never set.

    Firestore hands numbers back as int or float; going through str keeps a
    percentage like 0.75 from arriving as 0.7499999999999999.
    """
    return None if value is None else Decimal(str(value))


def _to_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    return None


def _to_profile(uid: str, data: dict[str, Any]) -> Profile:
    # Read from billings and merged in here. The wire shape is unchanged — the
    # client still receives one profile with a plan on it — but the record of
    # what someone pays for lives in its own collection.
    plan, plan_since = billing.get_billing(uid)
    balance = data.get("openingBalance")
    return Profile(
        uid=uid,
        email=data.get("email"),
        display_name=data.get("displayName", ""),
        photo_url=data.get("photoURL", ""),
        account_type=data.get("accountType", "individual"),
        timezone=data.get("timezone"),
        currency=data.get("currency"),
        opening_balance=Decimal(str(balance)) if balance is not None else None,
        trading_style=data.get("tradingStyle"),
        markets=list(data.get("markets", [])),
        bio=data.get("bio", ""),
        coach_language=data.get("coachLanguage"),
        market_type=data.get("marketType"),
        funding_type=data.get("fundingType"),
        prop_firm=data.get("propFirm", ""),
        account_size=_decimal(data.get("accountSize")),
        risk_per_trade_pct=_decimal(data.get("riskPerTradePct")),
        max_daily_loss_pct=_decimal(data.get("maxDailyLossPct")),
        target_r=_decimal(data.get("targetR")),
        max_trades_per_day=data.get("maxTradesPerDay"),
        strategies=list(data.get("strategies", [])),
        trading_rules=data.get("tradingRules", ""),
        plan=plan,
        plan_since=plan_since,
        created_at=_to_datetime(data.get("createdAt")),
        last_seen_at=_to_datetime(data.get("lastSeenAt")),
    )


def get_profile(uid: str) -> Profile:
    snapshot = profile_doc(uid).get()
    if not snapshot.exists:
        raise NotFoundError("No account record yet.")
    return _to_profile(uid, snapshot.to_dict() or {})


def record_sign_in(user: CurrentUser) -> Profile:
    """Upsert the account record from token claims.

    ``createdAt`` is only ever written when absent, so its presence is what
    marks an account as already known — the same rule the web client uses.
    """
    reference = profile_doc(user.uid)
    snapshot = reference.get()

    document: dict[str, Any] = {
        "email": user.email,
        "lastSeenAt": SERVER_TIMESTAMP,
    }
    if user.email_verified:
        document["verifiedAt"] = SERVER_TIMESTAMP
    if not snapshot.exists:
        document["createdAt"] = SERVER_TIMESTAMP
        document["displayName"] = user.name or ""

    reference.set(document, merge=True)

    # The public half of the same record, so contact search can find this
    # account by email. Written here because this is the one function every
    # sign-in passes through.
    directory.publish(user)

    return _to_profile(user.uid, reference.get().to_dict() or {})


def update_profile(uid: str, payload: ProfileUpdate) -> Profile:
    changes: dict[str, Any] = {}
    dumped = payload.model_dump(exclude_unset=True)

    for field, key in _FIELDS.items():
        if field not in dumped:
            continue
        value = dumped[field]
        changes[key] = float(value) if isinstance(value, Decimal) else value

    reference = profile_doc(uid)
    if changes:
        changes["updatedAt"] = SERVER_TIMESTAMP
        reference.set(changes, merge=True)

    snapshot = reference.get()
    if not snapshot.exists:
        raise NotFoundError("No account record yet.")

    profile = _to_profile(uid, snapshot.to_dict() or {})

    # Keep the searchable copy in step with the name and avatar just saved.
    directory.publish_fields(
        uid, display_name=profile.display_name, photo_url=profile.photo_url
    )

    return profile


def set_plan(uid: str, plan: PlanId) -> Profile:
    """Change the plan. Written to billings; the profile is untouched."""
    billing.set_plan(uid, plan)
    return _to_profile(uid, profile_doc(uid).get().to_dict() or {})


def is_confirmed(uid: str) -> bool:
    """Whether this account has confirmed its email address with us.

    Distinct from Firebase's email_verified, which Google sets by itself
    for an account that signed in through it. This flag is the one the app
    gates on, so a Google sign-in is held at the door like any other.
    """
    snapshot = profile_doc(uid).get()
    if not snapshot.exists:
        return False
    return bool((snapshot.to_dict() or {}).get("confirmedAt"))


def mark_confirmed(uid: str) -> None:
    """Record that the address was confirmed, now.

    Written only once: a second click on the same link should not move the
    date, because the date is a record of when it happened.
    """
    reference = profile_doc(uid)
    snapshot = reference.get()

    if snapshot.exists and (snapshot.to_dict() or {}).get("confirmedAt"):
        return

    reference.set({"confirmedAt": SERVER_TIMESTAMP}, merge=True)
