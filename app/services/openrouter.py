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
DEFAULT_MODELS = (
    "meta-llama/llama-3.2-3b-instruct",
    "nex-agi/nex-n2.5-mini:free",
    "nex-agi/nex-n2.5-pro:free",
)

# Some open models narrate their own thinking. None of it should reach a user.
_REASONING = re.compile(r"<(think|reasoning)>.*?</\1>", re.IGNORECASE | re.DOTALL)


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
    return _REASONING.sub("", text).strip()


async def complete(
    *,
    messages: list[dict[str, str]],
    settings: Settings,
    models: list[str] | None = None,
    temperature: float = 0.7,
    max_tokens: int = 1_200,
    json_object: bool = False,
) -> Completion:
    if settings.openrouter_api_key is None:
        raise AppError(
            "The coach is not configured on the server.",
            status_code=501,
            code="not_configured",
        )

    chain = models or configured_models(settings)
    body: dict[str, Any] = {
        "models": chain,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if json_object:
        body["response_format"] = {"type": "json_object"}

    try:
        timeout = settings.openrouter_timeout_seconds
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                _ENDPOINT,
                json=body,
                headers={
                    "Authorization": (
                        f"Bearer {settings.openrouter_api_key.get_secret_value()}"
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

    if response.status_code >= 400:
        # Body may quote the request; log it, never return it.
        logger.error(
            "OpenRouter returned %s",
            response.status_code,
            extra={"body": response.text[:500]},
        )
        raise UpstreamError("The coach could not answer just now.")

    try:
        payload = response.json()
        choice = payload["choices"][0]["message"]["content"]
    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
        logger.error("Unexpected OpenRouter payload shape")
        raise UpstreamError("The coach sent something unreadable.") from exc

    text = strip_reasoning(choice or "")
    if not text:
        raise UpstreamError("The coach returned an empty answer.")

    return Completion(text=text, model=payload.get("model", chain[0]))


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
