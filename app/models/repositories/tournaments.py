"""Tournaments, and who has entered them.

A tournament is a name and a window. Standings inside one are the points
earned *during that window* — scored by the same function the ladder uses,
handed a narrower slice of the same journal. That is deliberate: a second
scoring rule would be a second thing to keep honest, and a bracket that
rewarded something different from the ladder would teach two habits.

Entrants live in a subcollection keyed by uid, so entering twice is entering
once and "have I entered" is a document read.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from google.cloud.firestore_v1 import (
    SERVER_TIMESTAMP,
    DocumentSnapshot,
    Increment,
    Query,
)

from app.core.errors import NotFoundError
from app.db.firestore import get_client, tournaments_collection
from app.models.schemas.competition import Tournament

_ENTRANTS = "entrants"

MAX_TOURNAMENTS = 30


def _to_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    return None


def _state(starts: datetime | None, ends: datetime | None) -> str:
    """Derived from the clock, never stored.

    A stored state is a state something has to remember to change, and the
    thing that would have to remember is a scheduler this service does not
    have.
    """
    now = datetime.now(UTC)

    if starts is not None and now < starts:
        return "open"
    if ends is not None and now > ends:
        return "finished"
    return "running"


def _to_tournament(snapshot: DocumentSnapshot) -> Tournament:
    data = snapshot.to_dict() or {}
    starts = _to_datetime(data.get("startsAt"))
    ends = _to_datetime(data.get("endsAt"))

    return Tournament(
        id=snapshot.id,
        name=str(data.get("name", "")),
        blurb=str(data.get("blurb", "")),
        starts_at=starts,
        ends_at=ends,
        entrants=max(0, int(data.get("entrantCount", 0) or 0)),
        state=_state(starts, ends),  # type: ignore[arg-type]
    )


def listing() -> list[Tournament]:
    snapshot = (
        tournaments_collection()
        .order_by("startsAt", direction=Query.DESCENDING)
        .limit(MAX_TOURNAMENTS)
        .get()
    )

    return [_to_tournament(doc) for doc in snapshot]


def get(tournament_id: str) -> Tournament:
    snapshot = tournaments_collection().document(tournament_id).get()
    if not snapshot.exists:
        raise NotFoundError("No such tournament.")
    return _to_tournament(snapshot)


def has_entered(tournament_id: str, uid: str) -> bool:
    snapshot = (
        tournaments_collection()
        .document(tournament_id)
        .collection(_ENTRANTS)
        .document(uid)
        .get()
    )

    return bool(snapshot.exists)


def entered_ids(uid: str, tournament_ids: list[str]) -> set[str]:
    """Which of these the caller is in, in one batched read."""
    if not tournament_ids:
        return set()

    references = [
        tournaments_collection().document(entry).collection(_ENTRANTS).document(uid)
        for entry in tournament_ids
    ]

    return {
        doc.reference.parent.parent.id
        for doc in get_client().get_all(references)
        if doc.exists
    }


def enter(tournament_id: str, uid: str) -> None:
    """Join. The count moves only when the entrant is new, so entering twice
    cannot inflate a field."""
    reference = tournaments_collection().document(tournament_id)
    entrant = reference.collection(_ENTRANTS).document(uid)

    if entrant.get().exists:
        return

    entrant.set({"uid": uid, "enteredAt": SERVER_TIMESTAMP})

    reference.update({"entrantCount": Increment(1)})


def withdraw(tournament_id: str, uid: str) -> None:
    reference = tournaments_collection().document(tournament_id)
    entrant = reference.collection(_ENTRANTS).document(uid)

    if not entrant.get().exists:
        return

    entrant.delete()

    reference.update({"entrantCount": Increment(-1)})
