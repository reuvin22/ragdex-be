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
- At most three or four sentences unless they ask for more.
- Never show your reasoning or think out loud. Give the reply only."""

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


def headline_facts(summary: Summary) -> str:
    """The figures a coach reaches for most, already worked out.

    Handed over computed because small models derive them unreliably from the
    nested JSON that follows.
    """
    wins = round(summary.win_rate / 100 * summary.closed_count)
    lines = [
        f"Total trades logged: {summary.trade_count} ({summary.closed_count} closed)",
        f"Wins: {wins}. Losses: {summary.closed_count - wins}.",
        f"Win rate: {round(summary.win_rate)}%",
        f"Net P&L overall: {_money(summary.net_pl)}",
        f"Average winning trade: {_money(summary.avg_win)}",
        f"Average losing trade: -{_money(summary.avg_loss)}",
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
        else f"Position size change after a loss: {summary.after_loss.avg_size_change_pct}%",
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
            "Here is everything you know about their trading, computed from the trades\n"
            "they logged in this app. It is the only source you may draw on. These\n"
            "headline figures are already worked out — quote them, do not recalculate\n"
            "them:\n\n"
            f"{headline_facts(summary)}\n\n"
            "Full breakdown, for anything the headlines do not cover:\n\n"
            f"{json.dumps(asdict(summary), indent=1, default=str)}"
        )

    return f"""You are the AI Coach inside RagDex, a trading journal app. {who}

{REFUSAL_GUIDANCE}

This is the coaching playbook you work from. It is who you are:

{playbook()}

{OUTPUT_RULES}

Reply in {language}. Every word of it. If they write to you in a different
language, still reply in {language} unless they explicitly ask you to switch.

{data}"""


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
        {"role": "system", "content": build_system_prompt(summary, language, display_name)}
    ]
    for turn in history:
        messages.append(
            {"role": "user" if turn.role == "user" else "assistant", "content": turn.text}
        )
    messages.append({"role": "user", "content": message})

    return await openrouter.complete(messages=messages, settings=settings)
