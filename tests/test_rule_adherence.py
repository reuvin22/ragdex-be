"""The Rule Adherence Score: how a trader is graded on the part they control.

The journal already asked, per trade, whether the entry, exit and management
rules were kept. Those three answers sat in the summary as three unexplained
percentages. This turns them into one score, the band it falls in, and — the
part that actually changes behaviour — what breaking the rules has cost in
money.
"""

from __future__ import annotations

from decimal import Decimal

from app.models.schemas.trade import Trade
from app.services.coach import (
    build_system_prompt,
    discipline_verdict,
    rule_adherence_facts,
)
from app.services.stats import summarise


def _trade(index: int, net_pl: str, **rules) -> Trade:
    return Trade(
        id=f"t{index}",
        ticker="NVDA",
        direction="Long",
        net_pl=Decimal(net_pl),
        **rules,
    )


def _kept(index: int, net_pl: str) -> Trade:
    return _trade(
        index,
        net_pl,
        complied_entry="yes",
        complied_exit="yes",
        complied_management="yes",
    )


def _broke(index: int, net_pl: str) -> Trade:
    return _trade(
        index,
        net_pl,
        complied_entry="yes",
        complied_exit="no",
        complied_management="yes",
    )


def _mixed(kept: int, broken: int, win: str = "200", loss: str = "-900") -> list[Trade]:
    """`kept` trades that followed every rule, then `broken` that did not."""
    return [_kept(i, win) for i in range(kept)] + [
        _broke(i, loss) for i in range(kept, kept + broken)
    ]


def test_the_score_is_the_share_of_graded_trades_kept() -> None:
    trades = _mixed(8, 2, win="100", loss="-500")

    adherence = summarise(trades).rule_adherence

    assert adherence.tagged == 10
    assert adherence.followed == 8
    assert adherence.broken == 2
    assert adherence.score == 80.0


def test_one_broken_rule_breaks_the_trade() -> None:
    """Binary on purpose. Entering properly and exiting early is a broken
    trade, not two thirds of a kept one — partial credit makes the whole
    measurement soft."""
    adherence = summarise([_broke(0, "100")]).rule_adherence

    assert adherence.followed == 0
    assert adherence.broken == 1
    assert adherence.score == 0.0


def test_an_ungraded_trade_counts_neither_way() -> None:
    """A trade nobody graded is not evidence of anything."""
    trades = [_kept(0, "100"), _trade(1, "-100")]

    adherence = summarise(trades).rule_adherence

    assert adherence.tagged == 1
    assert adherence.score == 100.0


def test_no_grading_at_all_means_no_score() -> None:
    """Not a score of zero, which would read as total indiscipline rather than
    as an unanswered question."""
    adherence = summarise([_trade(0, "100"), _trade(1, "-50")]).rule_adherence

    assert adherence.score is None
    assert adherence.tagged == 0


def test_the_two_groups_are_priced_separately() -> None:
    """The comparison is the argument. The score on its own is just a number to
    feel bad about."""
    trades = _mixed(6, 3)

    adherence = summarise(trades).rule_adherence

    assert adherence.followed_side.net_pl == 1_200
    assert adherence.followed_side.win_rate == 100
    assert adherence.broken_side.net_pl == -2_700
    assert adherence.broken_side.avg_pl == -900


def test_the_bands_say_what_to_do_next() -> None:
    """"68%" says nothing actionable; "fix your worst trigger" does."""

    def rating(kept: int, total: int) -> str:
        pool = _mixed(kept, total - kept, win="10", loss="-10")
        return summarise(pool).rule_adherence.rating

    assert "System problem" in rating(5, 10)
    assert "Solid progress" in rating(7, 10)
    assert "Strong discipline" in rating(8, 10)
    assert "Elite discipline" in rating(9, 10)


def test_an_ungraded_rule_is_not_reported_as_zero_percent() -> None:
    """A real bug this sat next to. Nobody answering the question is not the
    same as answering "no" every time, and a flat 0% told the coach a trader
    never follows their entry rules on no evidence at all."""
    compliance = summarise([_trade(0, "100")]).plan_compliance

    assert compliance == {"entry": None, "exit": None, "management": None}


def test_the_facts_name_the_cost_of_breaking_the_rules() -> None:
    trades = _mixed(6, 3)

    lines = " ".join(rule_adherence_facts(summarise(trades).rule_adherence))

    assert "RULE ADHERENCE SCORE: 66.7%" in lines
    assert "FOLLOWED their rules" in lines
    assert "BROKE their rules" in lines
    assert "Breaking the rules has come to -2,700" in lines


def test_an_untagged_journal_is_told_to_start_tagging() -> None:
    lines = " ".join(rule_adherence_facts(summarise([_trade(0, "100")]).rule_adherence))

    assert "not measurable" in lines
    assert "single most useful thing they could start doing" in lines


def test_the_guidance_reaches_the_prompt() -> None:
    trades = _mixed(6, 3)
    prompt = " ".join(build_system_prompt(summarise(trades), "English", "Alex").split())

    assert "The Rule Adherence Score is how you grade a trader." in prompt
    assert "Discipline is a system, not a personality trait" in prompt
    assert "RULE ADHERENCE SCORE: 66.7%" in prompt


def test_the_grade_counts_discipline_alongside_the_money() -> None:
    """Money is the scoreboard and moves for reasons they do not own. An
    assessment that leaves out the part they control has graded the weather."""
    summary = summarise(_mixed(6, 3))
    prompt = " ".join(build_system_prompt(summary, "English", "A").split())

    assert "HOW THEY ARE EXECUTING: Rule Adherence Score 66.7%" in prompt
    assert "both count: the money AND the discipline score" in prompt
    assert "Grading someone on the money alone grades the weather" in prompt


def test_the_cost_of_the_score_rides_with_the_verdict() -> None:
    verdict = discipline_verdict(summarise(_mixed(6, 3)).rule_adherence)

    assert "-2,700" in verdict
    assert "the price of the score" in verdict


def test_an_ungraded_journal_says_the_money_is_all_there_is() -> None:
    verdict = discipline_verdict(summarise([_trade(0, "100")]).rule_adherence)

    assert "no discipline score" in verdict
    assert "the money is all you can judge until they start" in verdict
