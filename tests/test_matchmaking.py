"""Who gets paired with whom.

The pairing policy only. The transaction around it is what makes it safe
against two searches landing in the same second; this is what it decides.
"""

from __future__ import annotations

from app.models.repositories.battles import WIDEN_AFTER_SECONDS, pick_opponent

GOLD = "Gold"
SILVER = "Silver"
MASTER = "Master"


def test_an_empty_queue_pairs_with_nobody():
    assert pick_opponent([], tier=GOLD, waited=0) is None
    assert pick_opponent([], tier=GOLD, waited=600) is None


def test_own_bracket_is_taken_immediately():
    """No waiting for a peer. If one is there, that is the match."""
    picked = pick_opponent([("peer", GOLD)], tier=GOLD, waited=0)

    assert picked == "peer"


def test_another_bracket_is_not_taken_straight_away():
    """The thirty seconds exist so a same-tier opponent is found first."""
    assert pick_opponent([("higher", MASTER)], tier=GOLD, waited=0) is None
    assert (
        pick_opponent([("higher", MASTER)], tier=GOLD, waited=WIDEN_AFTER_SECONDS - 1)
        is None
    )


def test_the_search_widens_once_the_wait_is_up():
    picked = pick_opponent(
        [("higher", MASTER)], tier=GOLD, waited=WIDEN_AFTER_SECONDS
    )

    assert picked == "higher"


def test_a_peer_beats_a_longer_waiting_stranger():
    """The one case the ordering must not decide.

    A Silver who has queued for two minutes is ahead of the Gold in the list,
    and is still not the match — a ladder that pairs across brackets while a
    peer is available is not a ladder.
    """
    picked = pick_opponent(
        [("waited-ages", SILVER), ("peer", GOLD)], tier=GOLD, waited=600
    )

    assert picked == "peer"


def test_within_a_bracket_the_longest_wait_goes_first():
    picked = pick_opponent(
        [("first", GOLD), ("second", GOLD)], tier=GOLD, waited=0
    )

    assert picked == "first"


def test_widening_also_takes_the_longest_wait():
    picked = pick_opponent(
        [("first", SILVER), ("second", MASTER)],
        tier=GOLD,
        waited=WIDEN_AFTER_SECONDS + 10,
    )

    assert picked == "first"


def test_a_missing_tier_is_not_treated_as_a_match():
    """A row written before tiers existed must not read as everybody's bracket."""
    assert pick_opponent([("old", "")], tier=GOLD, waited=0) is None


def test_two_untiered_rows_still_pair():
    """Tiers are compared, not interpreted.

    In practice a tier always comes from ``rank_for`` and is never empty, so
    this is only the degenerate case: if two rows both lack one they count as
    the same bracket and pair at once, rather than both sitting in the queue.
    """
    assert pick_opponent([("old", "")], tier="", waited=0) == "old"
