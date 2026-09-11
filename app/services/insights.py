"""The behavioural leak analysis behind the dashboard card.

Kept apart from the request handler so it can be exercised without a signed-in
session, and so the prompt lives next to the parsing that depends on it.
"""

from __future__ import annotations

import logging

from app.core.config import Settings
from app.schemas.coach import LeakResult, Severity
from app.services import openrouter
from app.services.coach import headline_facts
from app.services.stats import MIN_TRADES_FOR_ANALYSIS, Summary

# The only values LeakResult accepts. Checked here because the model writes
# this field and a model will eventually write something else.
ALLOWED_SEVERITY: tuple[Severity, ...] = ("low", "medium", "high")


def _severity(value: str) -> Severity:
    """Narrowed by hand: the model writes this field, and a model will
    eventually write something that is not one of the three."""
    for allowed in ALLOWED_SEVERITY:
        if value == allowed:
            return allowed
    return "low"

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a trading performance coach analysing one trader's journal.

Name the single most expensive BEHAVIOURAL leak — a pattern in how they act, not
a market opinion. Good examples: revenge trading after losses, size creep,
cutting winners early, trading a losing session time, abandoning the plan under
stress, over-trading a setup that does not work for them.

Reply with a JSON object and nothing else, with exactly these keys:
  title           three or four words naming the leak
  finding         one or two plain sentences, using their real numbers
  costLabel       what it has cost, like "-$1,240 over 9 trades"
  severity        one of "low", "medium", "high"
  recommendation  one concrete thing to do before the next session

Never predict prices. Never recommend a trade. Only describe what they already
did."""


async def detect_leak(summary: Summary, settings: Settings) -> LeakResult | None:
    """None when there is not enough history to say anything honest."""
    if summary.trade_count < MIN_TRADES_FOR_ANALYSIS:
        return None

    completion = await openrouter.complete(
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": headline_facts(summary)},
        ],
        settings=settings,
        temperature=0.4,
        max_tokens=500,
        json_object=True,
    )

    payload = openrouter.parse_json_reply(completion.text)
    severity = str(payload.get("severity", "low")).lower()

    return LeakResult(
        title=str(payload.get("title", "Pattern found"))[:80],
        finding=str(payload.get("finding", ""))[:400],
        cost_label=str(payload.get("costLabel", ""))[:60],
        severity=_severity(severity),
        recommendation=str(payload.get("recommendation", ""))[:300],
    )
