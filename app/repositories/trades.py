"""Journal storage.

Every function here takes ``uid`` as its first argument and reaches the data
through ``trades_collection(uid)``. There is no code path that builds a query
across accounts, which is what keeps one trader's journal out of another's
even though the Admin SDK ignores Firestore rules.
"""

from __future__ import annotations

import base64
import binascii
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from google.cloud.firestore_v1 import SERVER_TIMESTAMP, DocumentSnapshot, Query

from app.core.errors import AppError, NotFoundError
from app.db.firestore import trades_collection
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


def _to_trade(snapshot: DocumentSnapshot) -> Trade:
    data = snapshot.to_dict() or {}
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

    query = trades_collection(uid).order_by(
        "createdAt", direction=Query.DESCENDING
    ).limit(limit + 1)

    if cursor:
        anchor = trades_collection(uid).document(_decode_cursor(cursor)).get()
        if not anchor.exists:
            raise AppError("That page is no longer available.", code="bad_cursor")
        query = query.start_after(anchor)

    snapshots = list(query.stream())
    has_more = len(snapshots) > limit
    page = snapshots[:limit]

    return [_to_trade(item) for item in page], _cursor_of(page[-1]) if has_more else None


def get_trade(uid: str, trade_id: str) -> Trade:
    snapshot = trades_collection(uid).document(trade_id).get()
    if not snapshot.exists:
        raise NotFoundError("That trade is not in your journal.")
    return _to_trade(snapshot)


def create_trade(uid: str, payload: TradeCreate) -> Trade:
    document = _derive(_to_document(payload, partial=False))
    document["createdAt"] = SERVER_TIMESTAMP
    document["updatedAt"] = SERVER_TIMESTAMP

    reference = trades_collection(uid).document()
    reference.set(document)
    return _to_trade(reference.get())


def update_trade(uid: str, trade_id: str, payload: TradeUpdate) -> Trade:
    reference = trades_collection(uid).document(trade_id)

    # Existence is checked against *this* user's subcollection, so a guessed
    # id from another account reads as "not found" rather than as a document.
    existing = reference.get()
    if not existing.exists:
        raise NotFoundError("That trade is not in your journal.")

    changes = _to_document(payload, partial=True)
    if not changes:
        return _to_trade(existing)

    # Derive from the merged view: a partial edit still has to produce a
    # correct P&L, and the fields it needs may not all be in the patch.
    merged = {**(existing.to_dict() or {}), **changes}
    for key in ("netPl", "riskReward"):
        merged.pop(key, None)
    derived = _derive(merged)

    changes["netPl"] = derived.get("netPl")
    changes["riskReward"] = derived.get("riskReward")
    changes["updatedAt"] = SERVER_TIMESTAMP

    reference.update(changes)
    return _to_trade(reference.get())


def delete_trade(uid: str, trade_id: str) -> None:
    reference = trades_collection(uid).document(trade_id)
    if not reference.get().exists:
        raise NotFoundError("That trade is not in your journal.")
    reference.delete()


def all_trades(uid: str, *, limit: int = 300) -> list[Trade]:
    """The window the coach and the analytics read. Bounded so one very long
    journal cannot turn a request into a scan of everything."""
    query = (
        trades_collection(uid)
        .order_by("createdAt", direction=Query.DESCENDING)
        .limit(min(limit, 1_000))
    )
    return [_to_trade(snapshot) for snapshot in query.stream()]
