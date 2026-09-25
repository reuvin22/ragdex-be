"""Billing records.

One document per account in ``billings``, keyed by uid. Separate from the
profile on purpose: what somebody is paying for and who they are answer
different questions, change at different times, and — the moment a payment
processor is connected — will want different retention and different access.
Keeping them apart now is cheaper than pulling them apart later.

No money moves through here. There is no processor connected; a plan is a
label the app gates features on.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast, get_args

from google.cloud.firestore_v1 import SERVER_TIMESTAMP

from app.db.firestore import billing_doc
from app.models.schemas.profile import PlanId

#: What an account without a billing record is on — every new sign-up, and
#: every account that has never chosen a plan.
DEFAULT_PLAN: PlanId = "free"


def _to_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    return None


def get_billing(uid: str) -> tuple[PlanId, datetime | None]:
    """The account's plan and when it started.

    Answers with the default rather than raising for an account that has never
    chosen one — every account has a plan, and the absence of a document means
    "the default", not "broken".
    """
    snapshot = billing_doc(uid).get()
    if not snapshot.exists:
        return DEFAULT_PLAN, None

    data = snapshot.to_dict() or {}
    plan = data.get("plan")
    return (
        # Read off PlanId rather than a second hand-kept list, so adding a plan
        # to the schema is the only change a new plan needs here.
        #
        # The cast states what the membership test just established. It is a
        # document field, so it arrives as Any, and `in get_args(PlanId)` is a
        # runtime check mypy cannot narrow through — the guard is the real
        # thing, and this only says so in the type.
        cast(PlanId, plan) if plan in get_args(PlanId) else DEFAULT_PLAN,
        _to_datetime(data.get("planSince")),
    )


def set_plan(uid: str, plan: PlanId) -> tuple[PlanId, datetime | None]:
    """Record the chosen plan, stamped with the server's clock."""
    reference = billing_doc(uid)
    reference.set({"uid": uid, "plan": plan, "planSince": SERVER_TIMESTAMP}, merge=True)

    data = reference.get().to_dict() or {}
    return plan, _to_datetime(data.get("planSince"))
