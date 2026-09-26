"""Who is in the arena, and how many points they hold.

One document per player, keyed by uid, and **a row means they have played a
match**. There is no joining step: the row is written when a match settles,
so the collection *is* the set of people eligible for the board.

That is what keeps the leaderboard a single ``order_by(points)`` with no
filter beside it. "Ranked players only" would otherwise be an equality filter
next to an ordering, which is the one shape that needs a composite index
deployed before the board works at all.

**No journal is read across accounts.** A trader's points are computed from
their own journal, by them, when they open the arena — and stored here. The
leaderboard reads stored numbers. That is not an optimisation; it is what
keeps a public board from being a reason for this service to hold a
cross-account read of the most private thing it stores.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from google.cloud.firestore_v1 import SERVER_TIMESTAMP, Increment, Query

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
        #: Matches played. A row only exists once this is at least one.
        self.matches = max(0, int(data.get("matches", 0) or 0))
        self.wins = max(0, int(data.get("wins", 0) or 0))
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


def record(uid: str, *, points: int, university_uid: str) -> None:
    """Update a score for somebody already on the board.

    A no-op for anybody who has not played: simply opening the arena must not
    put a name on a leaderboard of people who have competed.
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


def register(uid: str, *, points: int, university_uid: str, won: bool) -> None:
    """Put a result on the board, creating the row on a first match.

    This is the only thing that makes somebody rankable. It is called when a
    match settles, for both players, which is what makes the board a ranking
    of people who have actually played rather than of everybody who visited.
    """
    arena_collection().document(uid).set(
        {
            "uid": uid,
            "points": max(0, points),
            "universityUid": university_uid,
            "matches": Increment(1),
            "wins": Increment(1 if won else 0),
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
