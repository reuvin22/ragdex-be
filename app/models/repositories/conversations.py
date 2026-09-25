"""Coach conversation storage.

The conversation used to live only in React state. A refresh erased it, so the
coach opened every page load with no idea what had already been said and the
trader had to re-explain themselves. It is kept here instead.

That also closes a gap. The shape of the conversation was the one part of a
coach request the client supplied, so a forged history could put words in the
coach's mouth — not invent trades, the facts were always read server-side, but
enough to steer a reply. Now both halves come from the server.

Bounded on purpose. The newest ``TURN_LIMIT`` turns are kept and the oldest
fall off, because a conversation that grows forever stops fitting in a
Firestore document and in the model's context at roughly the same point.

Sealed like everything else that lands in a database: the text of what someone
told their coach about losing money is exactly the sort of thing that should
not be readable from the Firebase console.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.core.crypto import seal, unseal
from app.db.firestore import conversation_doc
from app.models.schemas.coach import MAX_REPLY, CoachTurn

_TURNS = "turns"

#: How many turns survive. Twenty exchanges is far more than the model is given
#: on any one request; the surplus is history for the trader to scroll, not
#: context for the coach.
TURN_LIMIT = 40


def _clamp(text: str) -> str:
    """Keep one turn inside the size the schema will re-validate on the way
    back out — a reply that could not be read again is worse than a short one."""
    return text[:MAX_REPLY]


def _to_turn(entry: Any) -> CoachTurn | None:
    """One stored entry, or None if it is not usable.

    Skipped rather than raised. A single unreadable turn — written under a key
    that has since changed, say — should cost that turn, not the conversation.
    """
    if not isinstance(entry, dict):
        return None

    role = entry.get("role")
    text = unseal(entry.get("text"))

    if role not in ("user", "coach") or not isinstance(text, str) or not text.strip():
        return None

    return CoachTurn(role=role, text=_clamp(text))


def load_turns(uid: str) -> list[CoachTurn]:
    """The stored conversation, oldest first. Empty when there is none."""
    snapshot = conversation_doc(uid).get()
    if not snapshot.exists:
        return []

    stored = (snapshot.to_dict() or {}).get(_TURNS) or []
    turns = (_to_turn(entry) for entry in stored)
    return [turn for turn in turns if turn is not None]


def store_exchange(uid: str, *, question: str, answer: str) -> None:
    """Append one question and the answer it got.

    Read-modify-write rather than ``ArrayUnion``, because the array has to be
    trimmed to the newest ``TURN_LIMIT`` in the same breath. Two tabs sending at
    the same instant could drop a turn; one person holding one conversation
    with one coach is not that race, and the cost of losing it is a line of
    chat history rather than anything derived from it.
    """
    now = datetime.now(UTC)

    stored = [
        {"role": turn.role, "text": seal(turn.text), "at": now}
        for turn in load_turns(uid)
    ]
    stored.append({"role": "user", "text": seal(_clamp(question)), "at": now})
    stored.append({"role": "coach", "text": seal(_clamp(answer)), "at": now})

    conversation_doc(uid).set({_TURNS: stored[-TURN_LIMIT:], "updatedAt": now})


def clear(uid: str) -> None:
    """Forget the conversation. Deletes the document rather than emptying it,
    so a trader who asked to be forgotten leaves nothing behind."""
    conversation_doc(uid).delete()
