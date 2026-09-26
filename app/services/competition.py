"""Assembling the arena.

The shape of this family is set by one decision: **a trader's points are
computed from their own journal, on their own request, and stored.** Every
board then reads stored numbers.

The alternative — scoring everybody when somebody opens a leaderboard — would
mean this service reading fifty strangers' journals to draw one page. It would
work, and it would make the most private collection in the product readable
across accounts for the sake of a scoreboard. Storing the number instead means
the only journal any request touches is the caller's own.

The cost is honest and worth naming: a board is as fresh as the last time each
trader opened the arena. Somebody who has not been back in a month is carrying
last month's score.
"""

from __future__ import annotations

from app.core.errors import AppError
from app.models.repositories import arena as arena_repo
from app.models.repositories import battles as battles_repo
from app.models.repositories import directory as directory_repo
from app.models.repositories import enrolment as enrolment_repo
from app.models.repositories import profiles as profiles_repo
from app.models.repositories import tournaments as tournaments_repo
from app.models.repositories import trades as trades_repo
from app.models.repositories import university_settings as settings_repo
from app.models.schemas.competition import (
    BATTLE_POINTS,
    DIVISIONS,
    Battle,
    BattleOpponent,
    Leaderboard,
    MyArena,
    Standing,
    Tournament,
    TournamentList,
    UniversityBoard,
    UniversityStanding,
)
from app.services.scoring import rank_for, score

#: How much of a journal a score is computed from.
#:
#: The same cap the roster summary uses, for the same reason: this runs on
#: every visit to the arena, and an unbounded read would make the page cost
#: grow with the length of somebody's career.
SCORE_SAMPLE = 400


def _university_of(uid: str) -> str:
    """The coach whose university this trader competes for, if any."""
    rows = enrolment_repo.coaches_of(uid)
    return rows[0].coach_uid if rows else ""


def refresh(uid: str) -> tuple[int, dict[str, int], str]:
    """Rescore the caller from their own journal, and store the result.

    Only ever the caller. Nothing in this module scores somebody else, which
    is what keeps a public board from needing a cross-account journal read.
    """
    items, _ = trades_repo.list_trades(uid, limit=SCORE_SAMPLE)
    points, breakdown = score(items)
    university = _university_of(uid)

    # A no-op for anybody who has not entered, so simply looking at the arena
    # does not put somebody on a ladder they did not join.
    arena_repo.record(uid, points=points, university_uid=university)

    return points, breakdown, university


def mine(uid: str) -> MyArena:
    """The caller's own standing, rescored on the way past."""
    points, breakdown, university = refresh(uid)
    entered = arena_repo.get(uid) is not None

    name = ""
    if university:
        name = settings_repo.get_settings(university).name

    return MyArena(
        entered=entered,
        rank=rank_for(points),
        position=arena_repo.position_of(uid, points) if entered else None,
        breakdown=breakdown,
        university_name=name,
    )


def leaderboard(uid: str) -> Leaderboard:
    """The top of the ladder, plus the caller's own row wherever it sits.

    Their own row is included even when it is nowhere near the top — a board
    that does not show you yourself is a board you have no reason to open
    twice.
    """
    entrants = arena_repo.top()
    people = directory_repo.get_many([entry.uid for entry in entrants])

    standings = [
        Standing(
            uid=entry.uid,
            display_name=people[entry.uid].display_name if entry.uid in people else "",
            photo_url=people[entry.uid].photo_url if entry.uid in people else "",
            rank=rank_for(entry.points),
            position=index,
        )
        for index, entry in enumerate(entrants, start=1)
    ]

    me = next((row for row in standings if row.uid == uid), None)

    if me is None:
        own = arena_repo.get(uid)
        if own is not None:
            person = directory_repo.get_many([uid]).get(uid)
            me = Standing(
                uid=uid,
                display_name=person.display_name if person else "",
                photo_url=person.photo_url if person else "",
                rank=rank_for(own.points),
                position=arena_repo.position_of(uid, own.points) or 0,
            )

    return Leaderboard(standings=standings, me=me)


def universities(uid: str) -> UniversityBoard:
    """University against university, as the sum of the members who entered.

    A sum rather than an average, deliberately. An average would make a
    university of one prodigy beat a university of thirty working traders,
    which is not what "our desk against yours" means to anybody who has said
    it.
    """
    entrants = arena_repo.all_entrants()

    totals: dict[str, int] = {}
    counts: dict[str, int] = {}

    for entry in entrants:
        if not entry.university_uid:
            continue
        totals[entry.university_uid] = totals.get(entry.university_uid, 0) + entry.points
        counts[entry.university_uid] = counts.get(entry.university_uid, 0) + 1

    ordered = sorted(totals.items(), key=lambda pair: pair[1], reverse=True)

    standings = [
        UniversityStanding(
            coach_uid=coach,
            name=settings_repo.get_settings(coach).name,
            entrants=counts[coach],
            points=points,
            position=index,
            rank=rank_for(points),
        )
        for index, (coach, points) in enumerate(ordered, start=1)
    ]

    own = _university_of(uid)
    mine_row = next((row for row in standings if row.coach_uid == own), None)

    return UniversityBoard(standings=standings, mine=mine_row)


def tournaments(uid: str) -> TournamentList:
    """What is running, and which of them the caller is in."""
    listing = tournaments_repo.listing()
    entered = tournaments_repo.entered_ids(uid, [entry.id for entry in listing])

    return TournamentList(
        tournaments=[
            Tournament(**{**entry.model_dump(), "entered": entry.id in entered})
            for entry in listing
        ]
    )


# ------------------------------------------------------------- the battle


def _relative(mine: int, theirs: int) -> str:
    """Where an opponent sits relative to you, by tier rather than by points.

    Tier, not raw points: a Gold I against a Gold III is the same bracket even
    though one has two hundred more points, and the table this was built from
    is about brackets.
    """
    ours = rank_for(mine).tier
    yours = rank_for(theirs).tier
    order = [name for name, _, _ in DIVISIONS]

    if order.index(yours) > order.index(ours):
        return "higher"
    if order.index(yours) < order.index(ours):
        return "lower"
    return "same"


def _window_return(uid: str, match: battles_repo.Match) -> float:
    """The caller's own percentage return inside the match window.

    **Percentage, not currency, and that is a departure from "most profit
    wins".** Two amounts from accounts of different sizes is not a contest —
    the larger account wins whatever either trader does, which would make a
    ladder built on it meaningless inside a week. A percentage asks the same
    question fairly, and it keeps the arena's rule that no figure in currency
    ever leaves an account.
    """
    if match.starts_at is None or match.ends_at is None:
        return 0.0

    items, _ = trades_repo.list_trades(uid, limit=SCORE_SAMPLE)

    inside = [
        trade
        for trade in items
        if trade.entry_at is not None
        and match.starts_at <= trade.entry_at <= match.ends_at
        and trade.net_pl is not None
    ]

    if not inside:
        return 0.0

    profit = float(sum(float(trade.net_pl or 0) for trade in inside))
    profile = profiles_repo.get_profile(uid)
    base = profile.account_size or profile.opening_balance or 0

    # No stated account size means no denominator, so the match falls back to
    # the sign of the result — better than dividing by a number nobody gave.
    if not base or float(base) <= 0:
        return 1.0 if profit > 0 else (-1.0 if profit < 0 else 0.0)

    return round(profit / float(base) * 100, 4)


def battle(uid: str) -> Battle:
    """The caller's match, from their own side.

    Settles on the way past when both sides have reported or the grace period
    has run out. There is no scheduler in this service, so the thing that
    finishes a match is somebody looking at it.
    """
    if battles_repo.waiting(uid):
        return Battle(state="searching")

    match = battles_repo.current(uid)
    if match is None:
        return Battle(state="idle")

    other = match.other(uid)
    person = directory_repo.get_many([other]).get(other)
    their_points = match.b_points if match.side(uid) == "a" else match.a_points

    opponent = BattleOpponent(
        uid=other,
        display_name=person.display_name if person else "",
        rank=rank_for(their_points),
    )

    remaining = 0
    if match.ends_at is not None:
        remaining = max(0, int((match.ends_at - battles_repo.now()).total_seconds()))

    if not match.settled and remaining > 0:
        return Battle(
            state="running",
            id=match.id,
            opponent=opponent,
            starts_at=match.starts_at,
            ends_at=match.ends_at,
            seconds_left=remaining,
        )

    if not match.settled:
        _try_settle(uid, match)
        refreshed = battles_repo.current(uid)
        if refreshed is not None:
            match = refreshed

    if not match.settled:
        return Battle(
            state="reporting",
            id=match.id,
            opponent=opponent,
            starts_at=match.starts_at,
            ends_at=match.ends_at,
            my_return=match.returns.get(uid),
            opponent_reported=match.reported(other),
        )

    return Battle(
        state="finished",
        id=match.id,
        opponent=opponent,
        starts_at=match.starts_at,
        ends_at=match.ends_at,
        won=match.winner == uid,
        points_delta=match.deltas.get(uid, 0),
        my_return=match.returns.get(uid),
        opponent_reported=True,
    )


def _try_settle(uid: str, match: battles_repo.Match) -> None:
    """Report this side, and close the match if it can be closed.

    The caller's figure is computed here from the caller's own journal. The
    opponent's is whatever they reported for themselves, which is why a match
    can only be decided once both have been back or the clock has run out on
    one of them.
    """
    if not match.reported(uid):
        battles_repo.report(match.id, uid, _window_return(uid, match))
        refreshed = battles_repo.current(uid)
        if refreshed is None:
            return
        match = refreshed

    other = match.other(uid)

    if not match.reported(other) and not battles_repo.past_grace(match):
        return

    mine = match.returns.get(uid) or 0.0
    theirs = match.returns.get(other)

    # A no-show forfeits rather than draws. Somebody who never came back
    # cannot be said to have traded to a tie.
    if theirs is None or mine > theirs:
        winner = uid
    elif theirs > mine:
        winner = other
    else:
        winner = ""

    if winner == "":
        # A genuine tie moves nobody. Paying both sides for a draw would make
        # a draw the most profitable outcome in the game.
        battles_repo.settle(match.id, winner="", deltas={uid: 0, other: 0})
        return

    loser = other if winner == uid else uid
    winner_points = match.a_points if match.side(winner) == "a" else match.b_points
    loser_points = match.a_points if match.side(loser) == "a" else match.b_points

    win_gain = BATTLE_POINTS[_relative(winner_points, loser_points)][0]
    lose_cost = BATTLE_POINTS[_relative(loser_points, winner_points)][1]

    battles_repo.settle(
        match.id, winner=winner, deltas={winner: win_gain, loser: lose_cost}
    )

    for player, delta in ((winner, win_gain), (loser, lose_cost)):
        entrant = arena_repo.get(player)
        if entrant is not None:
            arena_repo.record(
                player,
                points=max(0, entrant.points + delta),
                university_uid=entrant.university_uid,
            )


def search(uid: str) -> Battle:
    """Look for an opponent, or wait to be found."""
    entrant = arena_repo.get(uid)
    if entrant is None:
        raise AppError(
            "Enter the ladder before searching for a match.", code="not_entered"
        )

    existing = battles_repo.current(uid)
    if existing is not None and not existing.settled:
        return battle(uid)

    battles_repo.find_or_queue(uid, entrant.points)
    return battle(uid)


def cancel_search(uid: str) -> None:
    battles_repo.leave_queue(uid)
