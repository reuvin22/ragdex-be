"""Turning a journal into points.

The one rule this file exists to hold: **points must not be a proxy for
money.** If a score rose with P&L then publishing a leaderboard would publish
a ranking of account sizes, and the whole reason the arena scores points
rather than percentages would be lost.

So nothing here reads ``net_pl`` as a quantity. It reads it as a *sign* — a
trade was up or it was down — and everything else is about process: whether
the rules the trader wrote for themselves were followed, whether the journal
was actually kept, whether losses were cut rather than nursed. Two traders
with identical discipline score identically whether they risk fifty pounds a
trade or fifty thousand.

That is not a consolation prize for small accounts. It is the same thing the
coach measures and the same thing ``stats.rule_adherence`` already reports:
this product's position is that execution is the skill, and a ladder ought to
rank the skill.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from app.models.schemas.competition import DIVISIONS, Rank
from app.models.schemas.trade import Trade

#: A graded trade — one where the rule questions were answered — is worth
#: this on its own. Keeping the journal is the habit the whole product is
#: for, so it is the floor of the score.
POINTS_LOGGED = 2

#: Every rule kept on a trade. Three rules, so a perfectly executed trade is
#: worth nine points before anything else.
POINTS_RULE_KEPT = 3

#: A rule broken. Negative, and larger than a single rule kept, because the
#: habit is what is being scored and a break costs more than a keep earns.
POINTS_RULE_BROKEN = -4

#: A trade that went the right way. Small on purpose — a fifth of a clean
#: execution — so the ladder cannot be climbed on luck.
POINTS_WIN = 1

#: A day where every trade was fully compliant.
POINTS_CLEAN_DAY = 8

#: A day with a rule break on it. The mirror of the above.
POINTS_BROKEN_DAY = -6

#: How many trades in a day still count toward points.
#:
#: Without a cap the ladder rewards volume, and the quickest way up would be
#: to open forty positions and answer the rule questions forty times. That is
#: the opposite of what this is meant to encourage.
DAILY_TRADE_CAP = 10

#: Points never go below this. A bad month should cost a rank, not make the
#: ladder feel unrecoverable.
FLOOR = 0


def _answered(value: str) -> bool:
    return value in ("yes", "no")


def rank_for(points: int) -> Rank:
    """The badge for a score.

    Walks the ladder rather than dividing, because the divisions are not
    evenly spaced — the steps grow, so arithmetic would put people in the
    wrong tier at exactly the points where it matters most.
    """
    tier, division, floor = DIVISIONS[0]
    next_at: int | None = None

    for index, (name, number, threshold) in enumerate(DIVISIONS):
        if points < threshold:
            next_at = threshold
            break
        tier, division, floor = name, number, threshold
        next_at = DIVISIONS[index + 1][2] if index + 1 < len(DIVISIONS) else None

    del floor
    return Rank(tier=tier, division=division, points=points, next_at=next_at)


def score(
    trades: list[Trade],
    *,
    since: datetime | None = None,
    until: datetime | None = None,
) -> tuple[int, dict[str, int]]:
    """Points for a journal, and where they came from.

    The breakdown is returned with the total because a score nobody can
    account for is a score nobody trusts — and because "you lost points to
    rule breaks" is the one piece of feedback here worth acting on.

    ``since`` and ``until`` bound the window, which is what lets a tournament
    score the same journal over a fortnight without a second scoring rule.
    """
    parts: dict[str, int] = defaultdict(int)
    per_day: dict[str, list[Trade]] = defaultdict(list)

    for trade in trades:
        when = trade.entry_at
        if when is None:
            continue
        if since is not None and when < since:
            continue
        if until is not None and when > until:
            continue

        per_day[when.date().isoformat()].append(trade)

    for day in per_day.values():
        # Newest first is how the journal arrives; the cap should keep the
        # earliest trades of a day, which are the ones actually planned.
        counted = sorted(
            day, key=lambda t: t.entry_at or datetime.min
        )[:DAILY_TRADE_CAP]

        broke_today = False
        graded_today = False

        for trade in counted:
            answers = [
                trade.complied_entry,
                trade.complied_exit,
                trade.complied_management,
            ]
            graded = [value for value in answers if _answered(value)]

            if graded:
                graded_today = True
                parts["Journalled"] += POINTS_LOGGED

            for value in graded:
                if value == "yes":
                    parts["Rules kept"] += POINTS_RULE_KEPT
                else:
                    parts["Rules broken"] += POINTS_RULE_BROKEN
                    broke_today = True

            # Only on a graded trade. Awarding this for an ungraded one
            # would mean the ladder could be climbed without keeping the
            # journal — on the sign of a P&L alone, which is the one thing
            # this scoring is built not to reward.
            if graded and trade.net_pl is not None and trade.net_pl > 0:
                parts["Trades won"] += POINTS_WIN

        if not graded_today:
            continue

        if broke_today:
            parts["Days with a break"] += POINTS_BROKEN_DAY
        else:
            parts["Clean days"] += POINTS_CLEAN_DAY

    total = max(FLOOR, sum(parts.values()))

    # Dropped rather than shown as zero: a breakdown listing every category a
    # trader has not touched is noise around the two that matter.
    return total, {name: value for name, value in parts.items() if value != 0}
