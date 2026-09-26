"""The arena: points, ranks, and who is ahead.

**No money leaves this module.** A leaderboard row is a name, a rank and a
number of points — never a return, a balance or a P&L. That is the whole
reason points exist rather than percentages: a percentage published beside a
name is still a statement about somebody's finances, and a rank is not.

Points are earned for *how* somebody trades, not how much they make. That is
not a softening — it is the same thing the coach and the discipline score
already measure, and it is the only scoring that cannot be reverse-engineered
into an account size.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

#: The ladder, in order. Each tier holds three divisions.
TierName = Literal["Bronze", "Silver", "Gold", "Platinum", "Diamond", "Master"]

#: Where each division begins.
#:
#: The steps grow as the ladder does — climbing out of Bronze is meant to be
#: quicker than climbing out of Diamond, which is how a ladder stays worth
#: climbing. ``Master`` has one division because there is nothing above it.
DIVISIONS: list[tuple[TierName, int, int]] = [
    ("Bronze", 1, 0),
    ("Bronze", 2, 120),
    ("Bronze", 3, 260),
    ("Silver", 1, 420),
    ("Silver", 2, 600),
    ("Silver", 3, 800),
    ("Gold", 1, 1000),
    ("Gold", 2, 1200),
    ("Gold", 3, 1420),
    ("Platinum", 1, 1660),
    ("Platinum", 2, 1920),
    ("Platinum", 3, 2200),
    ("Diamond", 1, 2500),
    ("Diamond", 2, 2820),
    ("Diamond", 3, 3160),
    ("Master", 1, 3520),
]


class Rank(BaseModel):
    """A tier and a division, with the points that got there."""

    tier: TierName = "Bronze"
    #: 1, 2 or 3 — the stars on the badge.
    division: int = 1
    points: int = 0
    #: Where the next division starts, or null at the top of the ladder.
    next_at: int | None = None


class Standing(BaseModel):
    """One row of a leaderboard.

    Four fields, and the reason there are only four is the point of the whole
    design: a rank says somebody is good without saying what they are worth.
    """

    uid: str
    display_name: str = ""
    photo_url: str = ""
    rank: Rank = Field(default_factory=Rank)
    #: Position on the board, 1-based.
    position: int = 0


class Leaderboard(BaseModel):
    standings: list[Standing]
    #: The caller's own row, even when it falls outside the page above.
    me: Standing | None = None


class UniversityStanding(BaseModel):
    """A university's place, as the sum of its members."""

    coach_uid: str
    name: str = ""
    #: Members who have entered the arena. Not the roster size — somebody who
    #: has not entered is not carrying points for anybody.
    entrants: int = 0
    points: int = 0
    position: int = 0
    rank: Rank = Field(default_factory=Rank)


class UniversityBoard(BaseModel):
    standings: list[UniversityStanding]
    #: Where the caller's own university sits, if they are in one.
    mine: UniversityStanding | None = None


class Season(BaseModel):
    """The window points are counted over.

    A ladder nobody can fall off is a ladder that rewards having been here
    early. A season resets the board, so a run of good months stays worth
    making.
    """

    id: str = ""
    name: str = ""
    starts_at: datetime | None = None
    ends_at: datetime | None = None


class Tournament(BaseModel):
    """A bracket with a start and an end.

    Standings inside one are the points earned *during its window*, not the
    ladder — so a tournament is winnable by somebody who is Bronze on the
    ladder and had a very good fortnight.
    """

    id: str
    name: str
    blurb: str = ""
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    entrants: int = 0
    #: Whether the caller has entered.
    entered: bool = False
    #: open / running / finished, decided from the dates rather than stored.
    state: Literal["open", "running", "finished"] = "open"


class TournamentList(BaseModel):
    tournaments: list[Tournament]


class MyArena(BaseModel):
    """Everything the arena needs about the caller.

    One call, like the university inbox — the arena's header, badge and
    progress bar all need it, and three of them fetching separately was the
    mistake that endpoint already corrected.
    """

    #: False until they enter. Nobody is ranked without choosing to be.
    entered: bool = False
    rank: Rank = Field(default_factory=Rank)
    #: Where they sit on the global board, or null when unranked.
    position: int | None = None
    #: How the points were earned, so a score is never a black box.
    breakdown: dict[str, int] = Field(default_factory=dict)
    university_name: str = ""


class EnterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ------------------------------------------------------------- the battle

#: How long a match runs.
BATTLE_MINUTES = 10

#: How long after the bell both sides have to report before a no-show forfeits.
BATTLE_GRACE_MINUTES = 5

#: What a result is worth, by how the opponent ranked.
#:
#: The two cases given were "same bracket: +10 / -10" and "beat somebody
#: higher: +15, lose to them: -5". The third follows from those two and is
#: written out rather than left implied — beating somebody below you is worth
#: less, and losing to them costs more. Without it the quickest way up is to
#: farm weaker opponents.
BATTLE_POINTS: dict[str, tuple[int, int]] = {
    # opponent relative to you: (win, loss)
    "higher": (15, -5),
    "same": (10, -10),
    "lower": (5, -15),
}

BattleState = Literal["idle", "searching", "running", "reporting", "finished"]


class BattleOpponent(BaseModel):
    """Who you are against. A name and a badge, like every other standing."""

    uid: str = ""
    display_name: str = ""
    rank: Rank = Field(default_factory=Rank)


class Battle(BaseModel):
    """The caller's current match, from their own side.

    Deliberately one-sided. There is no field here for what the opponent
    scored until the match has settled, because until then it does not exist
    anywhere either — each side computes its own result from its own journal
    and reports it, so neither request ever reads the other's trades.
    """

    state: BattleState = "idle"
    id: str = ""
    opponent: BattleOpponent | None = None
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    #: Seconds left, so the client does not have to trust its own clock.
    seconds_left: int = 0

    #: Filled once settled.
    won: bool | None = None
    points_delta: int = 0
    #: Your own return over the window, as a percentage. Never the opponent's
    #: figure and never a currency amount.
    my_return: float | None = None
    #: Whether the opponent has reported yet. Not what they scored.
    opponent_reported: bool = False


class BattleSearch(BaseModel):
    model_config = ConfigDict(extra="forbid")
