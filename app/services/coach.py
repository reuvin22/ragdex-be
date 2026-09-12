"""The AI coach conversation.

Two constraints shape the prompt, the same two as the Node implementation it
mirrors. It stays inside this app — a trading journal is not a general
assistant, and a coach that answers anything invites questions it has no
business answering, like what to buy next. And it sounds like a person,
because the numbers are already on the dashboard.

The coach's voice lives in ``coach.md`` at the repository root, which this
module loads and the image ships beside ``app/``. Tuning the coach is editing
prose, not code.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict
from functools import lru_cache
from pathlib import Path

from app.core.config import Settings
from app.schemas.coach import CoachTurn
from app.services import openrouter
from app.services.stats import Summary

logger = logging.getLogger(__name__)

# ``coach.md`` sits at the root of whatever tree this package ships in: beside
# ``app/`` now that the backend is its own repository, and one level higher in
# the older layout where it lived in ``backend/``. Both are tried, so the file
# can move without this line having to move with it.
_PLAYBOOK_CANDIDATES = tuple(
    parent / "coach.md" for parent in Path(__file__).resolve().parents[2:4]
)
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)

REFUSAL_GUIDANCE = """You only discuss this trader's own journal: their logged
trades, their results, their habits, their psychology around trading, and how to
use this app.

If asked about anything else — general knowledge, news, coding, other people,
what to buy, where a market is heading, homework, recipes, anything at all
outside their own trading — decline warmly in one short sentence and offer
something you can help with instead. Do not answer the question even partially.
Do not explain that you are an AI or describe your restrictions in detail. Just
be a coach who talks about their trading and nothing else.

You never predict prices, never recommend a specific trade, and never tell them
what to buy or sell. You talk about patterns in what they have already done."""

OUTPUT_RULES = """How to format the reply:

- Plain conversational sentences only. Never use bullet points, headings, bold
  text, numbered lists, tables or markdown of any kind, however the playbook
  above is laid out — that is a document for you to read, not a style to copy.
  The app prints your reply as plain paragraphs, so a star or a hash lands on
  their screen as a star or a hash.
- Break it into short paragraphs, one idea each, separated by a blank line. Two
  or three paragraphs for a real question; a sentence or two for a small one.
- Write so that anyone can follow it — someone's parent, someone's younger
  brother, someone who started trading last month. No jargon. If a trading word
  is the only word that fits, say it and explain it in the same breath. "Your
  risk-reward is 0.6" means nothing; "you're risking a hundred to make sixty,
  which is backwards" means something.
- Never show your reasoning or think out loud. Give the reply only."""

JUDGEMENT_RULES = """How to read those numbers, because it is easy to get this
exactly backwards:

- The net P&L is the answer to "how am I doing". Nothing else is. If it is
  negative, this trader is losing money and your reply says so plainly, in the
  first sentence or two, before any encouragement.
- Win rate is how OFTEN they win, not how MUCH they keep. It is not a grade and
  it is not a headline. Someone can win two trades in three and still be down
  thousands, because the third one costs more than the two together — that is
  the single most common way a losing account looks healthy. Never quote a win
  rate approvingly without checking the average win against the average loss.
- The pair that actually matters is average win against average loss, and the
  profit factor that comes out of it. Below 1.0 means they pay out more than
  they take in, whatever the win rate says.
- "You are executing well" is about individual trades. It is never a verdict on
  an account that is down. You can tell someone their discipline is improving
  and that they are still losing money, in the same breath — that is honest,
  and it is the most useful thing you can say to them.
- Never soften the number. They can see their balance. A coach who tells them
  it looks fine is a coach they stop believing."""

BEHAVIOUR_EVIDENCE = """Some of those figures are not performance at all — they
are the behaviour itself, already measured. Read them that way:

- "Trades taken right after a loss", with what they came to together, is revenge
  trading with a price tag on it. It is the difference between "you chase losses"
  and "chasing losses has cost you this much".
- "Position size change after a loss" is the single most diagnostic number here.
  A positive figure means they size UP after losing — the exact mechanism that
  turns one ordinary losing trade into a bad week. Name it when you see it.
- "Median time back in after a loss" is how long they can sit still. Minutes
  means they never stopped trading; they just kept going while upset.
- "Plan compliance" separates the two failures for you. High compliance and poor
  results is a strategy problem. Poor compliance is an execution problem, and
  nothing about the strategy is worth discussing until it is fixed.
- "Worst setup by money" and "worst hour of the day" are where to point when they
  ask what to cut."""

ADVICE_SHAPE = """When you give advice, give the whole of it. Three parts, in
this order, written as flowing paragraphs and never as a list:

1. The one thing to do. Concrete enough to follow tomorrow — a number, a limit,
   a rule with an edge to it. "Stop revenge trading" is not advice; "when a
   trade loses, close the platform for twenty minutes before the next one" is.

2. What it does for them. Tie it to their own figures wherever the journal
   supports it: what this habit has already cost them, or what the month looks
   like without it. A reason they can feel beats a reason they have to trust.

3. What not to do — the thing that would quietly undo it. Every rule has a way
   of being followed in letter and broken in spirit: waiting the twenty minutes
   and then doubling the size, cutting the trade count and widening the stop,
   keeping the journal but only logging the wins. Name the specific one that
   goes with your advice, so they can see it coming.

Still one piece of advice per reply. Three fully explained parts of one change,
not three changes — a trader who leaves with three new rules follows none."""

FALLBACK_VOICE = """Talk like a real person who happens to coach traders.
Warm, direct, a little dry. Short sentences, ordinary words, their real numbers
rounded the way someone says them out loud. Grade how they executed, not what
they made. Never moralise, never guess, and never predict a price."""


@lru_cache
def playbook() -> str:
    """The coach's voice. Cached: it is the same for every request."""
    for candidate in _PLAYBOOK_CANDIDATES:
        try:
            text = _HTML_COMMENT.sub("", candidate.read_text(encoding="utf-8")).strip()
        except OSError:
            continue
        if text:
            return text

    logger.warning("coach.md could not be read; using the built-in voice.")
    return FALLBACK_VOICE


def _money(value: float) -> str:
    return f"{'-' if value < 0 else ''}{abs(round(value)):,}"


def standing(summary: Summary) -> str:
    """Whether this account is up or down, stated before anything else can
    soften it.

    Computed here rather than left for the model to infer. A coach reading a
    healthy-looking win rate off the top of the list would congratulate a
    trader who was three thousand dollars down — the win rate was true and the
    conclusion was nonsense. Win rate says how *often* they win, never how much
    they keep, and the two disagree the moment losers run bigger than winners.
    """
    if summary.closed_count == 0:
        return "Nothing is closed yet, so there is no result to judge."

    if summary.net_pl < 0:
        verdict = (
            f"THIS ACCOUNT IS DOWN {_money(abs(summary.net_pl))} OVERALL. "
            "They are losing money. Do not tell them they are doing well, doing "
            "fine, or on the right track, whatever any single statistic looks "
            "like on its own."
        )
        if summary.win_rate >= 50:
            verdict += (
                f" They win {round(summary.win_rate)}% of their trades and still "
                "lose money, because the average loss is bigger than the average "
                "win. That gap is the whole story — lead with it, not with the "
                "win rate."
            )
        return verdict

    if summary.net_pl == 0:
        return (
            "This account is exactly flat overall. They are not losing, but they "
            "are not being paid for the risk either."
        )

    return (
        f"This account is up {_money(summary.net_pl)} overall. Being up is not "
        "the same as being good — go looking for what is sloppy inside it."
    )


def headline_facts(summary: Summary) -> str:
    """The figures a coach reaches for most, already worked out.

    Handed over computed because small models derive them unreliably from the
    nested JSON that follows.

    Ordered deliberately. The bottom line comes first and the win rate comes
    last, sat next to the average win and loss that decide what it is worth —
    a lone "Win rate: 62%" near the top of a list reads as a verdict, and it is
    not one.
    """
    wins = round(summary.win_rate / 100 * summary.closed_count)
    per_trade = (
        summary.net_pl / summary.closed_count if summary.closed_count else 0.0
    )
    lines = [
        f"NET P&L OVERALL — the bottom line: {_money(summary.net_pl)}",
        f"Average result per closed trade: {_money(per_trade)}",
        f"Total trades logged: {summary.trade_count} ({summary.closed_count} closed)",
        f"Average winning trade: {_money(summary.avg_win)}",
        f"Average losing trade: -{_money(summary.avg_loss)}",
        (
            f"Wins: {wins}. Losses: {summary.closed_count - wins}. "
            f"That is a {round(summary.win_rate)}% win rate — how often they win, "
            "which says nothing on its own about whether they are making money."
        ),
        "Profit factor: not computable (no losses yet)"
        if summary.profit_factor is None
        else f"Profit factor: {summary.profit_factor}",
        "Average hold time: not recorded"
        if summary.avg_hold_minutes is None
        else f"Average hold time: {round(summary.avg_hold_minutes)} minutes",
        f"Worst losing streak: {summary.worst_streak} in a row",
        (
            f"Trades taken right after a loss: {summary.after_loss.count}, "
            f"together {_money(summary.after_loss.net_pl)}, winning "
            f"{round(summary.after_loss.win_rate)}% of the time"
        ),
        "Median time back in after a loss: not enough same-session data"
        if summary.after_loss.median_minutes_to_reentry is None
        else (
            "Median time back in after a loss (same session): "
            f"{summary.after_loss.median_minutes_to_reentry} minutes"
        ),
        "Position size change after a loss: not recorded"
        if summary.after_loss.avg_size_change_pct is None
        else (
            "Position size change after a loss: "
            f"{summary.after_loss.avg_size_change_pct}%"
        ),
        (
            "Plan compliance: entry {entry}%, exit {exit}%, management {management}%"
        ).format(**summary.plan_compliance),
    ]

    if summary.by_setup:
        worst = summary.by_setup[0]
        lines.append(
            f"Worst setup by money: {worst.label} — {worst.trades} trades, "
            f"{_money(worst.net_pl)}"
        )

    if summary.by_hour:
        worst_hour = summary.by_hour[0]
        lines.append(
            f"Worst hour of the day: {worst_hour.label} — {worst_hour.trades} trades, "
            f"{_money(worst_hour.net_pl)}"
        )

    return "\n".join(f"- {line}" for line in lines)


def language_rule(language: str) -> str:
    """The one instruction that has to survive a long prompt.

    Stated twice, and both times at an edge. Buried between the playbook and a
    multi-kilobyte stats dump it was reliably ignored by smaller models — the
    well-documented habit of attending to the start and end of a context and
    skimming the middle. So it closes the system prompt, and it is repeated in
    a short message placed after the conversation, immediately before the model
    generates, where nothing can bury it.

    Blunt on purpose. "Reply in Filipino" leaves room to answer in English
    about Filipino; naming the failure closes it.
    """
    # Skipped when the choice is English, where it would read as the nonsense
    # "do not write in English unless English is English" — a contradiction is
    # the last thing to hand a model you are already struggling to instruct.
    not_english = (
        "" if language.strip().lower() == "english" else "Do not write in English. "
    )

    return (
        f"LANGUAGE: write your entire reply in {language}. "
        f"Every sentence, including headings and any numbered points. "
        f"{not_english}"
        f"If the trader writes to you in another language, still answer in "
        f"{language}. "
        # The escape hatch this used to carry — "unless they explicitly ask you
        # to switch" — was a promise the system could not keep. The language is
        # pinned per request from the stored preference, so a switch the model
        # agreed to was undone on the very next turn: ask for Korean, get one
        # Korean reply at best and Tagalog again after. Sending them to the
        # control that actually changes it is the honest answer.
        f"If they ask you to reply in a different language, do not switch. "
        f"Tell them to use the language button above the conversation, which "
        f"changes it for good."
    )


def build_system_prompt(
    summary: Summary | None, language: str, display_name: str
) -> str:
    who = f"The trader's name is {display_name}." if display_name else ""

    if summary is None or summary.trade_count == 0:
        data = (
            "They have not logged enough trades yet for you to analyse anything. Be\n"
            "honest about that. Encourage them to log a few and tell them what you will\n"
            "be able to see once they do."
        )
    else:
        data = (
            f"WHERE THIS TRADER ACTUALLY STANDS: {standing(summary)}\n\n"
            "Here is everything you know about their trading, computed from the trades\n"
            "they logged in this app. It is the only source you may draw on. These\n"
            "headline figures are already worked out — quote them, do not recalculate\n"
            "them:\n\n"
            f"{headline_facts(summary)}\n\n"
            f"{JUDGEMENT_RULES}\n\n"
            f"{BEHAVIOUR_EVIDENCE}\n\n"
            "Full breakdown, for anything the headlines do not cover:\n\n"
            f"{json.dumps(asdict(summary), indent=1, default=str)}"
        )

    return f"""You are the AI Coach inside RagDex, a trading journal app. {who}

{REFUSAL_GUIDANCE}

This is the coaching playbook you work from. It is who you are:

{playbook()}

{OUTPUT_RULES}

{ADVICE_SHAPE}

{data}

{language_rule(language)}"""


async def ask(
    *,
    history: list[CoachTurn],
    message: str,
    summary: Summary | None,
    language: str,
    display_name: str,
    settings: Settings,
) -> openrouter.Completion:
    messages = [
        {
            "role": "system",
            "content": build_system_prompt(summary, language, display_name),
        }
    ]
    for turn in history:
        messages.append(
            {"role": "user" if turn.role == "user" else "assistant", "content": turn.text}
        )
    messages.append({"role": "user", "content": message})

    # Last, so it is the freshest thing in the context when generation starts.
    # The same rule is already at the end of the system prompt; a long history
    # pushes that far enough back that smaller models lose it again.
    messages.append({"role": "system", "content": language_rule(language)})

    return await openrouter.complete(messages=messages, settings=settings)
