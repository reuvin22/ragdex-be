"""Journal arithmetic.

A deliberate mirror of ``api/_trade-summary.ts``: the coach is given figures
that are already worked out, because small models derive them unreliably from
raw rows — one produced a confident "two out of three" for a 50% win rate.

Pure functions over a list of trades. No Firestore, no request, so this is the
part of the backend that is trivial to test.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from itertools import pairwise
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
class RuleSplit:
    """One side of the rules-followed / rules-broken comparison."""

    trades: int = 0
    net_pl: float = 0.0
    win_rate: float = 0.0
    profit_factor: float | None = None
    avg_pl: float = 0.0


@dataclass(slots=True)
class RuleAdherence:
    """How often this trader follows their own rules, and what it is worth.

    The score on its own is a number to feel bad about. The split beside it is
    the part that changes behaviour: the same trader, on the trades where they
    kept to their plan and the trades where they did not, with the difference
    in money. "You broke your rules on nine trades and they cost you $1,800" is
    an argument. "Be more disciplined" is not.

    Binary on purpose. A trade counts as followed only if every rule the trader
    answered for it was answered yes — entering early but exiting properly is
    broken, not two thirds kept. Partial credit creates a grey area that makes
    the whole measurement soft.
    """

    #: Trades with at least one rule question answered. The denominator — a
    #: trade nobody graded cannot count for or against.
    tagged: int = 0
    followed: int = 0
    broken: int = 0
    #: None when nothing has been tagged: no score rather than a score of zero,
    #: which would read as total indiscipline instead of no data.
    score: float | None = None
    rating: str = ""
    followed_side: RuleSplit = field(default_factory=RuleSplit)
    broken_side: RuleSplit = field(default_factory=RuleSplit)


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
    plan_compliance: dict[str, int | None] = field(default_factory=dict)
    rule_adherence: RuleAdherence = field(default_factory=RuleAdherence)
    by_setup: list[Bucket] = field(default_factory=list)
    by_hour: list[Bucket] = field(default_factory=list)


def _hold_minutes(trade: Trade) -> float | None:
    if not trade.entry_at or not trade.exit_at:
        return None
    delta = (trade.exit_at - trade.entry_at).total_seconds() / 60
    return delta if delta > 0 else None


def _when(trade: Trade) -> datetime | None:
    return trade.entry_at or trade.created_at


_RULES = ("complied_entry", "complied_exit", "complied_management")


def _compliance_rate(trades: list[Trade], attribute: str) -> int | None:
    """How often one rule was kept, or None if it was never graded.

    None rather than 0. Nobody answering the question is not the same as
    answering "no" every time, and the coach reading a flat zero would tell a
    trader they never follow their entry rules on the strength of no evidence
    whatsoever.
    """
    answered = [t for t in trades if getattr(t, attribute) in ("yes", "no")]
    if not answered:
        return None
    kept = sum(1 for t in answered if getattr(t, attribute) == "yes")
    return round(kept / len(answered) * 100)


def _rating(score: float) -> str:
    """The band a score falls in. Bands, not a raw number, because "68%" says
    nothing about what to do next and "solid progress, fix your worst trigger"
    does."""
    if score < 60:
        return (
            "System problem — the rules are too many, too vague, or not how "
            "they really trade"
        )
    if score < 75:
        return "Solid progress — following the rules more often than not"
    if score < 85:
        return "Strong discipline — into refinement, where each point is worth money"
    return "Elite discipline — the job now is holding it, and the risk is complacency"


def _split(trades: list[Trade]) -> RuleSplit:
    """The performance of one group of trades, closed ones only."""
    results = [float(t.net_pl) for t in trades if t.net_pl is not None]
    split = RuleSplit(trades=len(results))
    if not results:
        return split

    wins = [value for value in results if value >= 0]
    losses = [value for value in results if value < 0]
    gross_loss = abs(sum(losses))

    split.net_pl = round(sum(results), 2)
    split.avg_pl = round(sum(results) / len(results), 2)
    split.win_rate = len(wins) / len(results) * 100
    split.profit_factor = round(sum(wins) / gross_loss, 2) if gross_loss else None
    return split


def _rule_adherence(trades: list[Trade]) -> RuleAdherence:
    """The Rule Adherence Score, and what breaking the rules has cost.

    A trade is graded only if the trader answered at least one of the three
    rule questions on it, and it counts as followed only if every question they
    did answer was a yes.
    """
    adherence = RuleAdherence()

    graded: list[tuple[Trade, bool]] = []
    for trade in trades:
        answers = [
            answer
            for rule in _RULES
            if (answer := getattr(trade, rule)) in ("yes", "no")
        ]
        if answers:
            graded.append((trade, all(answer == "yes" for answer in answers)))

    if not graded:
        return adherence

    followed = [trade for trade, kept in graded if kept]
    broken = [trade for trade, kept in graded if not kept]

    adherence.tagged = len(graded)
    adherence.followed = len(followed)
    adherence.broken = len(broken)
    adherence.score = round(len(followed) / len(graded) * 100, 1)
    adherence.rating = _rating(adherence.score)
    adherence.followed_side = _split(followed)
    adherence.broken_side = _split(broken)

    return adherence


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
    summary.rule_adherence = _rule_adherence(trades)
    summary.by_setup = _worst_first(
        _bucket(trades, lambda t: t.setup.strip() or "Unlabelled")
    )
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

    for previous, current in pairwise(ordered):
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


def _bucket(
    trades: list[Trade], key: Callable[[Trade], str]
) -> dict[str, Bucket]:
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
