"""Journal storage.

Every function here takes ``uid`` as its first argument and reaches the data
through ``_owned(uid)``. There is no code path that builds a query
across accounts, which is what keeps one trader's journal out of another's
even though the Admin SDK ignores Firestore rules.
"""

from __future__ import annotations

import base64
import binascii
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast

from google.cloud.firestore_v1 import (
    SERVER_TIMESTAMP,
    DocumentSnapshot,
    FieldFilter,
    Query,
)

from app.core.crypto import seal_fields, unseal_fields
from app.core.errors import AppError, NotFoundError
from app.db.firestore import journal_collection
from app.schemas.trade import Trade, TradeCreate, TradeUpdate

MAX_PAGE_SIZE = 200


def _to_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (ValueError, ArithmeticError):
        return None


def _to_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    return None


# What gets encrypted, and — just as important — what does not.
#
# Everything a person wrote or that says what they traded is sealed. The four
# left out are the ones Firestore itself has to understand:
#
#   uid                  every query is filtered by it
#   createdAt/updatedAt  every list is ordered by it, and paging seeks on it
#
# Sealing those would hide little (a uid is not a secret; a timestamp is coarse)
# and would break listing and paging completely, because Firestore cannot order
# or range over ciphertext. That is the honest boundary of this approach: the
# fields you can query are the fields you cannot hide.
_SEALED = frozenset(
    {
        "ticker",
        "direction",
        "size",
        "sizeUnit",
        "entryPrice",
        "exitPrice",
        "entryAt",
        "exitAt",
        "setup",
        "rationale",
        "stopLoss",
        "takeProfit",
        "screenshot",
        "compliedEntry",
        "compliedExit",
        "compliedManagement",
        "emotionBefore",
        "emotionDuring",
        "mistakes",
        "netPl",
        "riskReward",
    }
)


def _readable(snapshot: DocumentSnapshot) -> dict[str, Any]:
    """The document as the rest of this module expects it: decrypted."""
    return unseal_fields(snapshot.to_dict() or {}, _SEALED)


def _to_trade(snapshot: DocumentSnapshot) -> Trade:
    data = _readable(snapshot)
    return Trade(
        id=snapshot.id,
        ticker=data.get("ticker", ""),
        direction=data.get("direction", "Long"),
        size=_to_decimal(data.get("size")),
        size_unit=data.get("sizeUnit", "Shares"),
        entry_price=_to_decimal(data.get("entryPrice")),
        exit_price=_to_decimal(data.get("exitPrice")),
        entry_at=_to_datetime(data.get("entryAt")),
        exit_at=_to_datetime(data.get("exitAt")),
        setup=data.get("setup", ""),
        rationale=data.get("rationale", ""),
        stop_loss=_to_decimal(data.get("stopLoss")),
        take_profit=_to_decimal(data.get("takeProfit")),
        screenshot=data.get("screenshot", ""),
        complied_entry=data.get("compliedEntry", ""),
        complied_exit=data.get("compliedExit", ""),
        complied_management=data.get("compliedManagement", ""),
        emotion_before=data.get("emotionBefore", ""),
        emotion_during=data.get("emotionDuring", ""),
        mistakes=list(data.get("mistakes", [])),
        net_pl=_to_decimal(data.get("netPl")),
        risk_reward=_to_decimal(data.get("riskReward")),
        created_at=_to_datetime(data.get("createdAt")),
        updated_at=_to_datetime(data.get("updatedAt")),
    )


def _to_document(payload: TradeCreate | TradeUpdate, *, partial: bool) -> dict[str, Any]:
    """Map snake_case fields onto the camelCase keys the client already reads.

    The web app talks to Firestore directly as well, so both writers have to
    agree on the document shape.
    """
    names = {
        "ticker": "ticker",
        "direction": "direction",
        "size": "size",
        "size_unit": "sizeUnit",
        "entry_price": "entryPrice",
        "exit_price": "exitPrice",
        "entry_at": "entryAt",
        "exit_at": "exitAt",
        "setup": "setup",
        "rationale": "rationale",
        "stop_loss": "stopLoss",
        "take_profit": "takeProfit",
        "screenshot": "screenshot",
        "complied_entry": "compliedEntry",
        "complied_exit": "compliedExit",
        "complied_management": "compliedManagement",
        "emotion_before": "emotionBefore",
        "emotion_during": "emotionDuring",
        "mistakes": "mistakes",
    }

    dumped = payload.model_dump(exclude_unset=partial)
    document: dict[str, Any] = {}

    for field, key in names.items():
        if field not in dumped:
            continue
        value = dumped[field]
        document[key] = float(value) if isinstance(value, Decimal) else value

    return document


def _derive(document: dict[str, Any]) -> dict[str, Any]:
    """Figures the server owns.

    Computed here rather than accepted from the client: a P&L a caller can
    set is a P&L that can be made up, and every statistic in the app is built
    on these two numbers.
    """
    entry = document.get("entryPrice")
    exit_price = document.get("exitPrice")
    size = document.get("size")
    stop = document.get("stopLoss")
    target = document.get("takeProfit")
    direction = document.get("direction", "Long")

    if entry is not None and exit_price is not None and size is not None:
        move = exit_price - entry if direction == "Long" else entry - exit_price
        document["netPl"] = round(move * size, 2)
    elif "exitPrice" in document and exit_price is None:
        # The trade was reopened; the old result no longer describes it.
        document["netPl"] = None

    if entry is not None and stop is not None and target is not None:
        risk = abs(entry - stop)
        reward = abs(target - entry)
        document["riskReward"] = round(reward / risk, 2) if risk else None

    return document


# The uid field is written by this module and never accepted from a payload.
# Every schema is extra="forbid" and none of them declares it, so there is no
# request shape that could set it — but it is named here so the reason is on
# the page rather than inferred.
_OWNER = "uid"


def _owned(uid: str) -> Any:
    """The journal, narrowed to one trader.

    The single place the ownership filter is written. `journal` is a flat
    collection, so nothing about its shape stops a query spanning accounts —
    what stops it is that every read below starts here and nowhere else.
    """
    return journal_collection().where(filter=FieldFilter(_OWNER, "==", uid))


def _owned_doc(uid: str, trade_id: str) -> DocumentSnapshot:
    """One trade, if it belongs to this trader.

    A document id is guessable in a flat collection in a way it was not when
    the journal lived under the account, so ownership is checked on read rather
    than assumed from the path. A trade belonging to someone else reads as
    absent, not as forbidden: which of the two it is, is not a caller's
    business.
    """
    snapshot: DocumentSnapshot = journal_collection().document(trade_id).get()
    if not snapshot.exists or (snapshot.to_dict() or {}).get(_OWNER) != uid:
        raise NotFoundError("That trade is not in your journal.")
    return snapshot


def _cursor_of(snapshot: DocumentSnapshot) -> str:
    return base64.urlsafe_b64encode(snapshot.id.encode()).decode()


def _decode_cursor(cursor: str) -> str:
    try:
        return base64.urlsafe_b64decode(cursor.encode()).decode()
    except (binascii.Error, UnicodeDecodeError) as exc:
        raise AppError("That page cursor is not valid.", code="bad_cursor") from exc


def list_trades(
    uid: str, *, limit: int = 50, cursor: str | None = None
) -> tuple[list[Trade], str | None]:
    limit = max(1, min(limit, MAX_PAGE_SIZE))

    # Needs the composite index in firestore.indexes.json: a filter on one
    # field ordered by another is not served by Firestore's automatic
    # single-field indexes.
    query = (
        _owned(uid)
        .order_by("createdAt", direction=Query.DESCENDING)
        .limit(limit + 1)
    )

    if cursor:
        # Through _owned_doc, so a cursor lifted from another account's page is
        # refused rather than used as a starting point in this one.
        anchor = _owned_doc(uid, _decode_cursor(cursor))
        query = query.start_after(anchor)

    snapshots = list(query.stream())
    has_more = len(snapshots) > limit
    page = snapshots[:limit]

    return [_to_trade(item) for item in page], _cursor_of(page[-1]) if has_more else None


def get_trade(uid: str, trade_id: str) -> Trade:
    return _to_trade(_owned_doc(uid, trade_id))


def create_trade(uid: str, payload: TradeCreate) -> Trade:
    document = _derive(_to_document(payload, partial=False))
    document["createdAt"] = SERVER_TIMESTAMP
    document["updatedAt"] = SERVER_TIMESTAMP

    # Stamped here, never taken from the payload. It is what every read filters
    # on, so a trade written without it belongs to nobody and is invisible.
    document[_OWNER] = uid

    reference = journal_collection().document()
    reference.set(seal_fields(document, _SEALED))
    return _to_trade(reference.get())


def update_trade(uid: str, trade_id: str, payload: TradeUpdate) -> Trade:
    existing = _owned_doc(uid, trade_id)
    reference = existing.reference

    changes = _to_document(payload, partial=True)
    if not changes:
        return _to_trade(existing)

    # Derive from the merged view: a partial edit still has to produce a
    # correct P&L, and the fields it needs may not all be in the patch. The
    # existing half is decrypted first, or the arithmetic would run on
    # ciphertext.
    merged = {**_readable(existing), **changes}
    for key in ("netPl", "riskReward"):
        merged.pop(key, None)
    derived = _derive(merged)

    changes["netPl"] = derived.get("netPl")
    changes["riskReward"] = derived.get("riskReward")
    changes["updatedAt"] = SERVER_TIMESTAMP

    reference.update(seal_fields(changes, _SEALED))
    # cast because the Firestore stubs type DocumentReference.get() as possibly
    # awaitable; this client is the synchronous one and never returns a future.
    return _to_trade(cast(DocumentSnapshot, reference.get()))


def delete_trade(uid: str, trade_id: str) -> None:
    _owned_doc(uid, trade_id).reference.delete()


def all_trades(uid: str, *, limit: int = 300) -> list[Trade]:
    """The window the coach and the analytics read. Bounded so one very long
    journal cannot turn a request into a scan of everything."""
    query = (
        _owned(uid)
        .order_by("createdAt", direction=Query.DESCENDING)
        .limit(min(limit, 1_000))
    )
    return [_to_trade(snapshot) for snapshot in query.stream()]
