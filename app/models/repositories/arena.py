"""Who is in the arena, and how many points they hold.

One document per entrant, keyed by uid, and **only entrants have one**.
Leaving deletes the row rather than flagging it, which buys the thing this
collection is for: the leaderboard is ``order_by(points)`` with no filter, so
Firestore's automatic single-field index serves it and there is no composite
index to deploy before the board works.

**No journal is read across accounts.** A trader's points are computed from
their own journal, by them, when they open the arena — and stored here. The
leaderboard reads stored numbers. That is not an optimisation; it is what
keeps a public board from being a reason for this service to hold a
cross-account read of the most private thing it stores.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from google.cloud.firestore_v1 import SERVER_TIMESTAMP, Query

from app.db.firestore import arena_collection

#: How many rows a board returns. A leaderboard is a top table, not a census.
BOARD_LIMIT = 50

#: How many entrants the university roll-up reads. Grouping happens in Python
#: over one query rather than a query per university.
ROLLUP_LIMIT = 500


class Entrant:
    def __init__(self, uid: str, data: dict[str, Any]) -> None:
        self.uid = uid
        self.points = max(0, int(data.get("points", 0) or 0))
        #: The coach whose university they were enrolled in when last scored.
        self.university_uid = str(data.get("universityUid", ""))
        raw = data.get("updatedAt")
        self.updated_at: datetime | None = (
            raw if isinstance(raw, datetime) else None
        )


def get(uid: str) -> Entrant | None:
    snapshot = arena_collection().document(uid).get()
    if not snapshot.exists:
        return None
    return Entrant(uid, snapshot.to_dict() or {})


def enter(uid: str) -> None:
    """Join the ladder. Idempotent — entering twice is still entered once."""
    arena_collection().document(uid).set(
        {"uid": uid, "joinedAt": SERVER_TIMESTAMP}, merge=True
    )


def leave(uid: str) -> None:
    """Leave, and be off the board immediately.

    Deleted rather than flagged. A flag would mean the leaderboard needed a
    filter beside its ordering, which is the one thing that would make it
    need a composite index.
    """
    arena_collection().document(uid).delete()


def record(uid: str, *, points: int, university_uid: str) -> None:
    """Store a score the trader's own request just computed.

    Only ever called for the caller's own uid. Nothing writes somebody else's
    row, which is why a leaderboard can be read without being trusted.
    """
    reference = arena_collection().document(uid)
    if not reference.get().exists:
        return

    reference.set(
        {
            "points": max(0, points),
            "universityUid": university_uid,
            "updatedAt": SERVER_TIMESTAMP,
        },
        merge=True,
    )


def top(limit: int = BOARD_LIMIT) -> list[Entrant]:
    """The board. One ordering, no filter, no composite index."""
    snapshot = (
        arena_collection()
        .order_by("points", direction=Query.DESCENDING)
        .limit(max(1, min(limit, BOARD_LIMIT)))
        .get()
    )

    return [Entrant(doc.id, doc.to_dict() or {}) for doc in snapshot]


def position_of(uid: str, points: int) -> int | None:
    """Where somebody sits, counted rather than scanned.

    A count of everybody above them plus one. Firestore answers an aggregate
    without reading the documents, so a trader deep down the ladder costs the
    same as one at the top.
    """
    if get(uid) is None:
        return None

    above = arena_collection().where("points", ">", points).count().get()

    try:
        return int(above[0][0].value) + 1
    except (IndexError, AttributeError, TypeError):
        # The aggregate shape differs across client versions, and a missing
        # position is a cosmetic loss — not a reason to fail the page.
        return None


def all_entrants(limit: int = ROLLUP_LIMIT) -> list[Entrant]:
    """Everybody, for the university roll-up.

    Capped, and the cap is honest: beyond it the university board is a sample
    rather than a standing. It is ordered by points so the sample is the top
    of the ladder rather than an arbitrary slice.
    """
    snapshot = (
        arena_collection()
        .order_by("points", direction=Query.DESCENDING)
        .limit(limit)
        .get()
    )

    return [Entrant(doc.id, doc.to_dict() or {}) for doc in snapshot]


def now() -> datetime:
    return datetime.now(UTC)
