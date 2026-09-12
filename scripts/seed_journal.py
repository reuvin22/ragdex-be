#!/usr/bin/env python
"""Fill a journal with plausible trades, so the coach has something to read.

    python scripts/seed_journal.py --email you@example.com            # preview
    python scripts/seed_journal.py --email you@example.com --write    # commit
    python scripts/seed_journal.py --email you@example.com --write --clear

Writes through ``repositories.trades.create_trade`` rather than building
documents by hand. That is the point: P&L, R:R and hold time are derived
server-side from entry, exit and size, and a seeder that wrote its own figures
would produce a journal whose numbers the app would never have computed —
which is exactly the journal you do not want to test analysis against.

The data is not noise. It carries three patterns on purpose, because a coach
with nothing to find says nothing worth reading:

  1. The first trade of the day loses. Entries before 09:45 are negative on
     average; everything later is positive.
  2. Losses are chased. The trade after a loss is roughly double size and wins
     far less often, and is tagged as such.
  3. Setups differ. "Opening Range Break" bleeds; "VWAP Reclaim" carries the
     account.

Deterministic: the same seed gives the same journal, so two runs can be
compared and a demo does not change under you.
"""

from __future__ import annotations

import argparse
import random
import sys
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

# Importable when run from the repository root without installing anything.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db.firestore import init_firebase
from app.repositories import trades as repo
from app.schemas.trade import TradeCreate
from firebase_admin import auth as firebase_auth

TICKERS = ("NVDA", "TSLA", "AAPL", "SPY", "AMD", "MSFT", "META", "COIN")

# Setup -> (share of trades, edge). Edge shifts the win probability, which is
# what makes one setup visibly better than another in the stats.
SETUPS = {
    "VWAP Reclaim": (0.30, 0.22),
    "Trend Pullback": (0.25, 0.10),
    "Breakout Retest": (0.20, 0.02),
    "Opening Range Break": (0.15, -0.26),
    "Mean Reversion": (0.10, -0.08),
}

CALM = ("Calm", "Focused", "Patient", "Neutral")
RATTLED = ("Frustrated", "Impatient", "Anxious", "Fear of missing out")

MISTAKES = (
    "Moved stop",
    "Oversized",
    "Revenge trade",
    "No plan",
    "Chased entry",
    "Cut winner early",
)

RATIONALES = (
    "Reclaimed VWAP on rising volume and held the retest.",
    "Third push into the level with a clean higher low.",
    "Broke the opening range and I did not wait for the retest.",
    "Faded the extension into the prior day high.",
    "Trend day; bought the first pullback to the 9 EMA.",
    "Saw the setup late and took it anyway.",
)


def _session_open(day: datetime) -> datetime:
    """09:30 New York, as UTC. Close enough for a seed, and no tz dependency."""
    return datetime.combine(day.date(), time(14, 30), tzinfo=UTC)


def _pick_setup(rng: random.Random) -> tuple[str, float]:
    roll = rng.random()
    running = 0.0
    for name, (share, edge) in SETUPS.items():
        running += share
        if roll <= running:
            return name, edge
    return "Trend Pullback", 0.10


def _build(rng: random.Random, days: int) -> list[TradeCreate]:
    trades: list[TradeCreate] = []
    today = datetime.now(UTC)

    for offset in range(days, 0, -1):
        day = today - timedelta(days=offset)
        if day.weekday() >= 5:  # No weekend sessions.
            continue
        if rng.random() < 0.25:  # Not every weekday is traded.
            continue

        session = _session_open(day)
        last_was_loss = False

        for index in range(rng.randint(1, 4)):
            setup, edge = _pick_setup(rng)

            # Minutes after the open. The first trade of the day is early by
            # construction — that is the habit being modelled.
            minutes = rng.randint(1, 12) if index == 0 else rng.randint(20, 330)
            entry_at = session + timedelta(minutes=minutes)
            held = rng.randint(4, 95)
            exit_at = entry_at + timedelta(minutes=held)

            # The three patterns, as probabilities rather than hard rules, so
            # the journal reads like a person rather than a spreadsheet.
            win_chance = 0.52 + edge
            if minutes < 15:
                win_chance -= 0.30
            if last_was_loss:
                win_chance -= 0.18

            won = rng.random() < max(0.05, min(0.95, win_chance))

            entry_price = Decimal(str(round(rng.uniform(28, 540), 2)))
            # Chasing a loss shows up as size, which is the tell worth finding.
            base_size = rng.choice((50, 75, 100, 150, 200))
            size = Decimal(base_size * 2 if last_was_loss else base_size)

            move = Decimal(str(round(rng.uniform(0.3, 4.2), 2)))
            direction = "Long" if rng.random() < 0.7 else "Short"
            gain = move if won else -move * Decimal(str(round(rng.uniform(0.7, 1.6), 2)))
            exit_price = entry_price + (gain if direction == "Long" else -gain)

            risk = Decimal(str(round(rng.uniform(0.5, 2.5), 2)))
            stop = entry_price - (risk if direction == "Long" else -risk)
            target = entry_price + (risk * 2 if direction == "Long" else -risk * 2)

            broke_rules = last_was_loss or minutes < 15
            tags: list[str] = []
            if last_was_loss:
                tags += ["Revenge trade", "Oversized"]
            if minutes < 15 and not won:
                tags.append("Chased entry")
            if broke_rules and rng.random() < 0.4:
                tags.append(rng.choice(MISTAKES))

            trades.append(
                TradeCreate(
                    ticker=rng.choice(TICKERS),
                    direction=direction,
                    size=size,
                    size_unit="Shares",
                    entry_price=entry_price,
                    exit_price=max(Decimal("0.01"), exit_price),
                    entry_at=entry_at,
                    exit_at=exit_at,
                    setup=setup,
                    rationale=rng.choice(RATIONALES),
                    stop_loss=max(Decimal("0.01"), stop),
                    take_profit=max(Decimal("0.01"), target),
                    complied_entry="no" if broke_rules else "yes",
                    complied_exit="no" if not won and rng.random() < 0.5 else "yes",
                    complied_management="no" if broke_rules else "yes",
                    emotion_before=rng.choice(RATTLED if last_was_loss else CALM),
                    emotion_during=rng.choice(RATTLED if not won else CALM),
                    # De-duplicated by the schema, so repeats here are harmless.
                    mistakes=tags,
                )
            )

            last_was_loss = not won

    return trades


def _clear(uid: str) -> int:
    """Delete this trader's journal, and only theirs.

    Through the repository's own owned-query helper rather than a raw
    collection scan. `journal` is flat now, so a delete loop that forgot the
    uid filter would empty everybody's journal rather than one.
    """
    removed = 0

    while True:
        page = list(repo._owned(uid).limit(400).stream())
        if not page:
            return removed
        for document in page:
            document.reference.delete()
            removed += 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    identity = parser.add_mutually_exclusive_group(required=True)
    identity.add_argument("--email", help="Account to seed, by email address.")
    identity.add_argument("--uid", help="Account to seed, by Firebase uid.")
    parser.add_argument("--days", type=int, default=90, help="How far back to go.")
    parser.add_argument(
        "--count",
        type=int,
        help=(
            "Keep only the most recent N trades. The generator works day by day, "
            "so trimming from the front preserves whole sessions and the patterns "
            "that run through them."
        ),
    )
    parser.add_argument("--seed", type=int, default=7, help="Random seed.")
    parser.add_argument(
        "--clear",
        action="store_true",
        help="Delete the existing journal first. Not undoable.",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Actually write. Without this the run only reports what it would do.",
    )
    args = parser.parse_args()

    init_firebase()

    if args.uid:
        uid = args.uid
        who = uid
    else:
        record = firebase_auth.get_user_by_email(args.email)
        uid = record.uid
        who = f"{record.email} ({uid})"

    planned = _build(random.Random(args.seed), args.days)
    if args.count:
        planned = planned[-args.count :]
    wins = sum(1 for t in planned if (t.exit_price or 0) != (t.entry_price or 0))

    print(f"Account: {who}")
    print(f"Trades:  {len(planned)} across {args.days} days ({wins} closed)")

    if not args.write:
        print("\nDry run. Nothing was written. Re-run with --write to commit.")
        return 0

    if args.clear:
        removed = _clear(uid)
        print(f"Cleared:  {removed} existing trades")

    for index, payload in enumerate(planned, start=1):
        repo.create_trade(uid, payload)
        if index % 20 == 0:
            print(f"  written {index}/{len(planned)}")

    print(f"\nDone. {len(planned)} trades written to {who}.")
    print("The coach reads the journal server-side, so it can see them now.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
