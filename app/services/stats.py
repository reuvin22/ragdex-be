"""Journal arithmetic.

A deliberate mirror of ``api/_trade-summary.ts``: the coach is given figures
that are already worked out, because small models derive them unreliably from
raw rows — one produced a confident "two out of three" for a 50% win rate.

Pure functions over a list of trades. No Firestore, no request, so this is the
part of the backend that is trivial to test.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from statistics import median

from app.schemas.trade import Trade

# Below this, any "pattern" is noise.
MIN_TRADES_FOR_ANALYSIS = 8


def _as_float(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


@dataclass(slots=True)
class Bucket:
    label: str
    trades: int = 0
    net_pl: float = 0.0
    wins: int = 0


@dataclass(slots=True)
class AfterLoss:
    count: int = 0
    net_pl: float = 0.0
    win_rate: float = 0.0
    median_minutes_to_reentry: float | None = None
    avg_size_change_pct: float | None = None


@dataclass(slots=True)
class Summary:
    trade_count: int = 0
    closed_count: int = 0
    win_rate: float = 0.0
    net_pl: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    profit_factor: float | None = None
    avg_hold_minutes: float | None = None
    worst_streak: int = 0
    after_loss: AfterLoss = field(default_factory=AfterLoss)
    plan_compliance: dict[str, int] = field(default_factory=dict)
    by_setup: list[Bucket] = field(default_factory=list)
    by_hour: list[Bucket] = field(default_factory=list)


def _hold_minutes(trade: Trade) -> float | None:
    if not trade.entry_at or not trade.exit_at:
        return None
    delta = (trade.exit_at - trade.entry_at).total_seconds() / 60
    return delta if delta > 0 else None


def _when(trade: Trade) -> datetime | None:
    return trade.entry_at or trade.created_at


def _compliance_rate(trades: list[Trade], attribute: str) -> int:
    answered = [t for t in trades if getattr(t, attribute) in ("yes", "no")]
    if not answered:
        return 0
    kept = sum(1 for t in answered if getattr(t, attribute) == "yes")
    return round(kept / len(answered) * 100)


def summarise(trades: list[Trade]) -> Summary:
    """Everything the coach and the leak detector are allowed to know."""
    summary = Summary(trade_count=len(trades))
    if not trades:
        return summary

    closed = [t for t in trades if t.net_pl is not None]
    results = [float(t.net_pl) for t in closed]  # type: ignore[arg-type]

    summary.closed_count = len(closed)
    summary.net_pl = round(sum(results), 2)

    wins = [value for value in results if value >= 0]
    losses = [value for value in results if value < 0]

    if closed:
        summary.win_rate = len(wins) / len(closed) * 100
    summary.avg_win = round(sum(wins) / len(wins), 2) if wins else 0.0
    summary.avg_loss = round(abs(sum(losses) / len(losses)), 2) if losses else 0.0

    gross_loss = abs(sum(losses))
    summary.profit_factor = (
        round(sum(wins) / gross_loss, 2) if gross_loss else None
    )

    holds = [minutes for t in trades if (minutes := _hold_minutes(t)) is not None]
    summary.avg_hold_minutes = round(sum(holds) / len(holds), 1) if holds else None

    # Chronological for anything that depends on sequence.
    ordered = sorted(
        [t for t in closed if _when(t) is not None],
        key=lambda t: _when(t),  # type: ignore[arg-type,return-value]
    )

    streak = worst = 0
    for trade in ordered:
        if (trade.net_pl or 0) < 0:
            streak += 1
            worst = max(worst, streak)
        else:
            streak = 0
    summary.worst_streak = worst

    summary.after_loss = _after_loss(ordered)
    summary.plan_compliance = {
        "entry": _compliance_rate(trades, "complied_entry"),
        "exit": _compliance_rate(trades, "complied_exit"),
        "management": _compliance_rate(trades, "complied_management"),
    }
    summary.by_setup = _worst_first(_bucket(trades, lambda t: t.setup.strip() or "Unlabelled"))
    summary.by_hour = _worst_first(
        _bucket(
            [t for t in trades if _when(t)],
            lambda t: f"{_when(t).hour:02d}:00",  # type: ignore[union-attr]
        )
    )

    return summary


def _after_loss(ordered: list[Trade]) -> AfterLoss:
    """What happens in the trade immediately following a loser.

    This is the single most useful behavioural cut in the journal, which is
    why it gets its own figures rather than living inside a generic bucket.
    """
    stats = AfterLoss()
    gaps: list[float] = []
    size_changes: list[float] = []

    for previous, current in zip(ordered, ordered[1:]):
        if (previous.net_pl or 0) >= 0:
            continue

        stats.count += 1
        stats.net_pl += float(current.net_pl or 0)
        if (current.net_pl or 0) >= 0:
            stats.win_rate += 1

        before, after = _when(previous), _when(current)
        if before and after:
            minutes = (after - before).total_seconds() / 60
            # Same session only: an overnight gap says nothing about tilt.
            if 0 < minutes <= 8 * 60:
                gaps.append(minutes)

        previous_size = _as_float(previous.size)
        current_size = _as_float(current.size)
        if previous_size and current_size:
            size_changes.append((current_size - previous_size) / previous_size * 100)

    if stats.count:
        stats.net_pl = round(stats.net_pl, 2)
        stats.win_rate = stats.win_rate / stats.count * 100

    stats.median_minutes_to_reentry = round(median(gaps), 1) if gaps else None
    stats.avg_size_change_pct = (
        round(sum(size_changes) / len(size_changes), 1) if size_changes else None
    )
    return stats


def _bucket(trades: list[Trade], key) -> dict[str, Bucket]:
    buckets: dict[str, Bucket] = defaultdict(lambda: Bucket(label=""))
    for trade in trades:
        label = key(trade)
        bucket = buckets[label]
        bucket.label = label
        bucket.trades += 1
        if trade.net_pl is not None:
            bucket.net_pl = round(bucket.net_pl + float(trade.net_pl), 2)
            if trade.net_pl >= 0:
                bucket.wins += 1
    return buckets


def _worst_first(buckets: dict[str, Bucket]) -> list[Bucket]:
    """Most expensive first: the coach is looking for what is costing money,
    not for what is working."""
    return sorted(buckets.values(), key=lambda bucket: bucket.net_pl)
