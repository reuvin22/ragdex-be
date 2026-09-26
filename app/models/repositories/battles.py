"""Matchmaking, and the matches it makes.

Two collections. ``battle-queue`` holds one document per trader waiting for an
opponent, and pairing *deletes* both — so a document in the queue means
"still waiting", with no flag to go stale. ``battles`` holds the match.

**Pairing runs in a transaction.** Two traders pressing search in the same
second would otherwise both read the same waiting opponent and both claim
them, and the loser of that race ends up in a match their opponent does not
know about. The transaction is the only reason this is safe, so it is not an
optimisation to be simplified away later.

**A match stores each side's result separately**, written only by that side.
Nothing here reads a second account's journal: each player computes their own
return from their own trades and reports it, which is what keeps a head-to-head
from being a reason for one trader's request to open another's journal.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from google.cloud.firestore_v1 import SERVER_TIMESTAMP, Query, transactional

from app.db.firestore import battle_queue, battles_collection, get_client
from app.models.schemas.competition import BATTLE_GRACE_MINUTES, BATTLE_MINUTES


def now() -> datetime:
    return datetime.now(UTC)


def _at(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    return None


class Match:
    """One battle, read from whichever side is asking."""

    def __init__(self, match_id: str, data: dict[str, Any]) -> None:
        self.id = match_id
        self.a_uid = str(data.get("aUid", ""))
        self.b_uid = str(data.get("bUid", ""))
        self.symbol = str(data.get("symbol", ""))
        self.a_points = int(data.get("aPoints", 0) or 0)
        self.b_points = int(data.get("bPoints", 0) or 0)
        self.starts_at = _at(data.get("startsAt"))
        self.ends_at = _at(data.get("endsAt"))
        self.settled = bool(data.get("settled", False))
        self.winner = str(data.get("winner", ""))
        self.returns: dict[str, float | None] = {
            self.a_uid: _number(data.get("aReturn")),
            self.b_uid: _number(data.get("bReturn")),
        }
        self.deltas: dict[str, int] = {
            self.a_uid: int(data.get("aDelta", 0) or 0),
            self.b_uid: int(data.get("bDelta", 0) or 0),
        }

    def other(self, uid: str) -> str:
        return self.b_uid if uid == self.a_uid else self.a_uid

    def side(self, uid: str) -> str:
        return "a" if uid == self.a_uid else "b"

    def reported(self, uid: str) -> bool:
        return self.returns.get(uid) is not None


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


# ------------------------------------------------------------------ queue


def waiting(uid: str) -> bool:
    return bool(battle_queue().document(uid).get().exists)


def leave_queue(uid: str) -> None:
    battle_queue().document(uid).delete()


#: How long somebody waits for their own bracket before the search widens.
#:
#: A ladder that only ever pairs inside a tier is a ladder where a Master
#: never gets a game. Thirty seconds is long enough that a same-bracket
#: opponent who is also searching will be found first, and short enough that
#: nobody sits staring at a spinner.
WIDEN_AFTER_SECONDS = 30

#: How deep into the queue to look. Small: the tier filter runs in Python
#: over these rather than as a second `where`, because an equality filter
#: beside the `joinedAt` ordering is what would need a composite index.
QUEUE_DEPTH = 25


def pick_opponent(
    candidates: list[tuple[str, str]], *, tier: str, waited: float
) -> str | None:
    """Who to pair with, out of the queue as it stands.

    Separated from the transaction because it is the whole policy and the
    transaction is only the safety around it. ``candidates`` is (uid, tier),
    longest wait first, the caller already removed.

    Own bracket always wins, however long anybody has waited — a Silver who
    has been queuing two minutes still does not get handed a Master if another
    Silver is there. Only when the caller's own bracket is empty *and* they
    have waited ``WIDEN_AFTER_SECONDS`` does anybody else become eligible.
    """
    for uid, entry_tier in candidates:
        if entry_tier == tier:
            return uid

    if waited < WIDEN_AFTER_SECONDS:
        return None

    return candidates[0][0] if candidates else None


def find_or_queue(
    uid: str, points: int, *, symbol: str, tier: str
) -> Match | None:
    """Claim an opponent in the caller's bracket, or join the queue.

    Returns the match when one was made, and None when the caller is now the
    one waiting.

    Same bracket first. Somebody who has been queuing longer than
    ``WIDEN_AFTER_SECONDS`` will take anybody — otherwise a tier with one
    player in it is a tier nobody can play in.

    Runs in a transaction because the read of "who is waiting" and the write
    that claims them have to be one operation. Without it two searches a
    moment apart both pair with the same person, and one of them ends up in a
    match their opponent knows nothing about.
    """
    client = get_client()
    match_id = uuid.uuid4().hex

    @transactional
    def _pair(transaction: Any) -> dict[str, Any] | None:
        # The longest wait first: somebody who has been queuing for a minute
        # should not be skipped for somebody who just arrived.
        candidates = [
            doc
            for doc in battle_queue()
            .order_by("joinedAt")
            .limit(QUEUE_DEPTH)
            .stream(transaction=transaction)
            if doc.id != uid
        ]

        mine = battle_queue().document(uid).get(transaction=transaction)
        waited = 0.0
        if mine.exists:
            joined = _at((mine.to_dict() or {}).get("joinedAt"))
            if joined is not None:
                waited = (now() - joined).total_seconds()

        chosen = pick_opponent(
            [
                (doc.id, str((doc.to_dict() or {}).get("tier", "")))
                for doc in candidates
            ],
            tier=tier,
            waited=waited,
        )
        opponent = next((doc for doc in candidates if doc.id == chosen), None)

        if opponent is None:
            # Keep the original joinedAt, or somebody polling every four
            # seconds resets their own wait and never widens.
            if not mine.exists:
                transaction.set(
                    battle_queue().document(uid),
                    {
                        "uid": uid,
                        "points": points,
                        "tier": tier,
                        "joinedAt": SERVER_TIMESTAMP,
                    },
                )
            return None

        opponent_data = opponent.to_dict() or {}
        started = now()

        payload = {
            "aUid": uid,
            "bUid": opponent.id,
            "symbol": symbol,
            "aPoints": points,
            "bPoints": int(opponent_data.get("points", 0) or 0),
            "startsAt": started,
            "endsAt": started + timedelta(minutes=BATTLE_MINUTES),
            "settled": False,
            "winner": "",
            "aReturn": None,
            "bReturn": None,
            "aDelta": 0,
            "bDelta": 0,
            "players": [uid, opponent.id],
        }

        # Both leave the queue as part of the same write, so neither can be
        # claimed a second time by a search that is already in flight.
        transaction.delete(battle_queue().document(opponent.id))
        transaction.delete(battle_queue().document(uid))
        transaction.set(battles_collection().document(match_id), payload)

        return payload

    payload = _pair(client.transaction())
    return None if payload is None else Match(match_id, payload)


# ----------------------------------------------------------------- matches


def current(uid: str) -> Match | None:
    """The caller's live match, if any.

    Unsettled ones only, newest first. A settled match is history and is
    fetched by id when the result screen wants it.
    """
    snapshot = (
        battles_collection()
        .where("players", "array_contains", uid)
        .order_by("startsAt", direction=Query.DESCENDING)
        .limit(1)
        .get()
    )

    for doc in snapshot:
        return Match(doc.id, doc.to_dict() or {})

    return None


def report(match_id: str, uid: str, percent: float) -> None:
    """Record one side's own result. Only ever called for that side."""
    reference = battles_collection().document(match_id)
    snapshot = reference.get()
    if not snapshot.exists:
        return

    match = Match(match_id, snapshot.to_dict() or {})
    reference.set({f"{match.side(uid)}Return": percent}, merge=True)


def settle(match_id: str, *, winner: str, deltas: dict[str, int]) -> None:
    reference = battles_collection().document(match_id)
    snapshot = reference.get()
    if not snapshot.exists:
        return

    match = Match(match_id, snapshot.to_dict() or {})

    reference.set(
        {
            "settled": True,
            "winner": winner,
            "aDelta": deltas.get(match.a_uid, 0),
            "bDelta": deltas.get(match.b_uid, 0),
            "settledAt": SERVER_TIMESTAMP,
        },
        merge=True,
    )


def past_grace(match: Match) -> bool:
    """Whether a no-show has run out of time to report."""
    if match.ends_at is None:
        return False

    return now() > match.ends_at + timedelta(minutes=BATTLE_GRACE_MINUTES)
