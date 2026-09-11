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
from app.db.firestore import user_doc
from app.schemas.profile import PlanId, Profile, ProfileUpdate

from . import directory

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
}


def _to_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    return None


def _to_profile(uid: str, data: dict[str, Any]) -> Profile:
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
        plan=data.get("plan", "individual"),
        plan_since=_to_datetime(data.get("planSince")),
        created_at=_to_datetime(data.get("createdAt")),
        last_seen_at=_to_datetime(data.get("lastSeenAt")),
    )


def get_profile(uid: str) -> Profile:
    snapshot = user_doc(uid).get()
    if not snapshot.exists:
        raise NotFoundError("No account record yet.")
    return _to_profile(uid, snapshot.to_dict() or {})


def record_sign_in(user: CurrentUser) -> Profile:
    """Upsert the account record from token claims.

    ``createdAt`` is only ever written when absent, so its presence is what
    marks an account as already known — the same rule the web client uses.
    """
    reference = user_doc(user.uid)
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

    reference = user_doc(uid)
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
    reference = user_doc(uid)
    reference.set({"plan": plan, "planSince": SERVER_TIMESTAMP}, merge=True)
    return _to_profile(uid, reference.get().to_dict() or {})
