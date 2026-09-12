"""OpenRouter client.

The key is a real secret. It is read from settings, sent only to OpenRouter,
and never returned, logged or echoed in an error — upstream failures are
logged with their detail and surfaced to the caller as a generic message.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

import httpx

from app.core.config import Settings
from app.core.errors import AppError, UpstreamError

logger = logging.getLogger(__name__)

_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"

# Free models rate-limit hard and independently, so a request carries a list
# and OpenRouter falls through on 429. Kept in step with api/_openrouter.ts.
# Free models lead, deliberately. These are the fallback when OPENROUTER_MODEL
# is unset, and an unconfigured deployment is exactly the one least likely to
# have credit on the account — leading with a paid model meant the default
# answered 402 rather than working. A paid model belongs in OPENROUTER_MODEL,
# chosen on purpose, not in the path taken when nobody chose anything.
DEFAULT_MODELS = (
    "nex-agi/nex-n2.5-mini:free",
    "nex-agi/nex-n2.5-pro:free",
    "meta-llama/llama-3.2-3b-instruct",
)

# How far the budget may be stretched when a reply is cut off before it starts.
# A ceiling rather than no limit: past this the problem is the prompt, not the
# room, and doubling forever only makes each failure slower and dearer.
_RETRY_CEILING = 8_000

# Some open models narrate their own thinking. None of it should reach a user.
_REASONING = re.compile(r"<(think|reasoning)>.*?</\1>", re.IGNORECASE | re.DOTALL)

# A reply cut off by the token budget can open a reasoning block and never close
# it. The paired pattern above needs both tags, so it does not match a truncated
# one — and without this the trader reads the model's raw internal monologue,
# which is a worse failure than the empty answer it would otherwise have been.
_UNCLOSED_REASONING = re.compile(r"<(think|reasoning)>.*\Z", re.IGNORECASE | re.DOTALL)


@dataclass(slots=True)
class Completion:
    text: str
    model: str


def configured_models(settings: Settings) -> list[str]:
    """A single slug or a comma-separated fallback chain."""
    if not settings.openrouter_model:
        return list(DEFAULT_MODELS)
    return [
        slug.strip() for slug in settings.openrouter_model.split(",") if slug.strip()
    ]


def strip_reasoning(text: str) -> str:
    """Remove a model's own thinking, closed or truncated."""
    return _UNCLOSED_REASONING.sub("", _REASONING.sub("", text)).strip()


async def _post(
    *, body: dict[str, Any], settings: Settings, chain: list[str]
) -> dict[str, Any]:
    """One call to OpenRouter, with every transport and status failure already
    turned into the error a caller should raise."""
    try:
        timeout = settings.openrouter_timeout_seconds
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                _ENDPOINT,
                json=body,
                headers={
                    "Authorization": (
                        f"Bearer {settings.openrouter_api_key.get_secret_value()}"  # type: ignore[union-attr]
                    ),
                    "Content-Type": "application/json",
                },
            )
    except httpx.TimeoutException as exc:
        logger.warning(
            "OpenRouter timed out after %ss", settings.openrouter_timeout_seconds
        )
        raise AppError(
            "The coach took too long to answer. Try a shorter question.",
            status_code=504,
            code="upstream_timeout",
        ) from exc
    except httpx.HTTPError as exc:
        logger.warning("OpenRouter transport error: %s", type(exc).__name__)
        raise UpstreamError("Could not reach the coach.") from exc

    if response.status_code == 429:
        raise AppError(
            "The coach is rate limited right now. Try again shortly.",
            status_code=429,
            code="rate_limited",
        )

    if response.status_code == 402:
        # Not a fault, a bill. Every paid model answers 402 on an account with
        # no credit, and the generic message sent whoever deployed this looking
        # for a bug that was not there.
        logger.error(
            "OpenRouter refused for payment (402). The configured models are %s — "
            "a paid model needs credit on the OpenRouter account, and a chain "
            "ending in a ':free' model degrades instead of failing.",
            ", ".join(chain),
        )
        raise AppError(
            "The coach is out of credit. Ask whoever runs this to top up "
            "OpenRouter or switch to a free model.",
            status_code=502,
            code="no_credit",
        )

    if response.status_code >= 400:
        # Body may quote the request; log it, never return it.
        logger.error(
            "OpenRouter returned %s",
            response.status_code,
            extra={"body": response.text[:500]},
        )
        raise UpstreamError("The coach could not answer just now.")

    try:
        payload: dict[str, Any] = response.json()
        return payload
    except json.JSONDecodeError as exc:
        logger.error("OpenRouter sent a body that is not JSON")
        raise UpstreamError("The coach sent something unreadable.") from exc


async def complete(
    *,
    messages: list[dict[str, str]],
    settings: Settings,
    models: list[str] | None = None,
    temperature: float = 0.7,
    max_tokens: int | None = None,
    json_object: bool = False,
) -> Completion:
    if settings.openrouter_api_key is None:
        raise AppError(
            "The coach is not configured on the server.",
            status_code=501,
            code="not_configured",
        )

    chain = models or configured_models(settings)
    budget = max_tokens or settings.openrouter_max_tokens

    body: dict[str, Any] = {
        "models": chain,
        "messages": messages,
        "temperature": temperature,
    }
    if json_object:
        body["response_format"] = {"type": "json_object"}

    while True:
        # The reasoning models in the default chain spend this budget thinking
        # before they write a word, and it covers both. Too small and the
        # thinking finishes, the answer never starts, and nothing comes back.
        body["max_tokens"] = budget

        payload = await _post(body=body, settings=settings, chain=chain)

        try:
            choice = payload["choices"][0]
            message = choice["message"]
        except (KeyError, IndexError, TypeError) as exc:
            logger.error("Unexpected OpenRouter payload shape")
            raise UpstreamError("The coach sent something unreadable.") from exc

        content = message.get("content") or ""
        text = strip_reasoning(content)

        if text:
            return Completion(text=text, model=payload.get("model", chain[0]))

        # Nothing usable. Three causes, three different fixes — this used to
        # raise with no log line at all, which left one opaque sentence and no
        # way to tell them apart.
        finish = choice.get("finish_reason")
        # Reasoning models can return their thinking in its own field and leave
        # content empty. It is counted here and shown to nobody.
        reasoning = message.get("reasoning") or ""
        truncated = finish == "length"

        if truncated:
            diagnosis = "The budget ran out before the answer began."
        elif reasoning or content:
            diagnosis = "The model returned only its own reasoning, never an answer."
        else:
            diagnosis = "The model returned an empty message."

        logger.error(
            "OpenRouter returned nothing usable: model=%s finish_reason=%s "
            "content=%d chars reasoning=%d chars budget=%d. %s",
            payload.get("model", chain[0]),
            finish,
            len(content),
            len(reasoning),
            budget,
            diagnosis,
        )

        # One more go with room to finish. A model that thinks its way through
        # the whole budget is a number that was too small, not a broken coach,
        # and a slower reply beats an error. Doubling on demand also keeps the
        # default from having to cover the worst case on every request.
        if truncated and budget < _RETRY_CEILING:
            budget = min(budget * 2, _RETRY_CEILING)
            logger.warning("Retrying the same request with %d tokens", budget)
            continue

        if truncated:
            raise UpstreamError(
                "The coach ran out of room before it could answer. Try a shorter "
                "question."
            )
        raise UpstreamError("The coach returned an empty answer.")


def parse_json_reply(text: str) -> dict[str, Any]:
    """Pull a JSON object out of a model reply.

    Models wrap JSON in prose or fences however they like, so the first
    balanced object in the string is taken rather than trusting the whole
    response to parse.
    """
    try:
        parsed: dict[str, Any] = json.loads(text)
        return parsed
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        raise UpstreamError("The coach did not return usable analysis.")

    try:
        salvaged: dict[str, Any] = json.loads(text[start : end + 1])
        return salvaged
    except json.JSONDecodeError as exc:
        raise UpstreamError("The coach did not return usable analysis.") from exc
