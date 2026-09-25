"""The stored behavioural leak, and when it is due to be worked out again.

The analysis used to run on every dashboard load. That spent a model call each
time to re-derive a habit that moves over weeks, and on a free-tier key it is
also the quickest way to be rate limited out of the feature entirely.

So it is computed on a cadence the trader chooses and kept here in between.
The cadence is not only about cost: it decides what the finding is *about*. A
daily read speaks to yesterday's session; a monthly one speaks to a pattern,
and re-running it every few minutes would only produce a slightly different
sentence about the same twenty trades.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.core.crypto import seal_fields, unseal_fields
from app.db.firestore import insights_collection
from app.models.schemas.coach import LeakResult
from app.models.schemas.profile import LeakCadence

logger = logging.getLogger(__name__)

#: One document, replaced each time. There is only ever one current leak, and
#: keeping a history would mean deciding how long to keep it.
_DOCUMENT = "behavioral-leak"

# The model wrote every one of these about this trader's own behaviour, which
# is as sensitive as anything else in the journal.
_SEALED = frozenset({"title", "finding", "costLabel", "severity", "recommendation"})


def _zone(timezone: str | None) -> tzinfo:
    """The trader's timezone, or UTC if they have not set one or it is junk.

    It matters for "daily": end of day means the end of *their* day, and a
    trader in Manila reading a leak stamped by a UTC boundary would see it
    refresh in the middle of the afternoon.
    """
    if not timezone:
        return UTC
    try:
        return ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning("Unknown timezone on a profile: %r", timezone)
        return UTC


def next_due(
    computed_at: datetime, cadence: LeakCadence, timezone: str | None
) -> datetime:
    """When an analysis made at ``computed_at`` stops being current.

    Calendar boundaries rather than a rolling window, because that is what the
    words mean. "Every week" is a new week, not a hundred and sixty-eight hours
    after whenever they last happened to open the dashboard — which would drift
    the refresh a little later every time until it landed at midnight.
    """
    zone = _zone(timezone)
    local = computed_at.astimezone(zone)
    day = local.date()

    if cadence == "weekly":
        # Monday, counting the current day as part of this week.
        start = day + timedelta(days=7 - day.weekday())
    elif cadence == "monthly":
        start = (
            date(day.year + 1, 1, 1)
            if day.month == 12
            else date(day.year, day.month + 1, 1)
        )
    else:
        start = day + timedelta(days=1)

    return datetime(start.year, start.month, start.day, tzinfo=zone)


def load(uid: str) -> tuple[LeakResult, datetime, int] | None:
    """The stored leak, when it was written, and how many trades it covered."""
    snapshot = insights_collection(uid).document(_DOCUMENT).get()
    if not snapshot.exists:
        return None

    data = unseal_fields(snapshot.to_dict() or {}, _SEALED)
    computed_at = data.get("computedAt")
    if not isinstance(computed_at, datetime):
        return None

    try:
        result = LeakResult(
            title=data.get("title", ""),
            finding=data.get("finding", ""),
            cost_label=data.get("costLabel", ""),
            severity=data.get("severity", "low"),
            recommendation=data.get("recommendation", ""),
        )
    except ValueError:
        # Written under an older shape, or under a key that has since changed.
        # Recomputing is cheap next to serving nonsense.
        logger.warning("Stored leak for %s could not be read back", uid)
        return None

    if computed_at.tzinfo is None:
        computed_at = computed_at.replace(tzinfo=UTC)

    return result, computed_at, int(data.get("tradeCount", 0))


def store(uid: str, result: LeakResult, *, trade_count: int) -> datetime:
    """Save a freshly computed leak. Returns the moment it was stamped."""
    computed_at = datetime.now(UTC)

    document = seal_fields(
        {
            "title": result.title,
            "finding": result.finding,
            "costLabel": result.cost_label,
            "severity": result.severity,
            "recommendation": result.recommendation,
        },
        _SEALED,
    )
    document["computedAt"] = computed_at
    document["tradeCount"] = trade_count

    insights_collection(uid).document(_DOCUMENT).set(document)
    return computed_at
