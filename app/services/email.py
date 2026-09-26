"""Transactional email, through Brevo.

The Brevo key can send mail as you, so it is a real secret: read from settings,
sent only to Brevo, and never logged or returned. Brevo echoes the key in some
error payloads, which is why a rejection is logged by status alone.

The address a message goes to is never taken from a request body — it comes
from the account record the caller's verified session names. Otherwise this
endpoint would be a way to aim mail at someone else's inbox from our domain.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from app.core.config import Settings
from app.core.errors import AppError, UpstreamError
from app.models.schemas.university import UniversitySettings
from app.views.email_template import verification_html, verification_text
from app.views.invitation_email import invitation_html, invitation_text

logger = logging.getLogger(__name__)

_ENDPOINT = "https://api.brevo.com/v3/smtp/email"
_TIMEOUT = httpx.Timeout(20.0, connect=10.0)

# One verification email a minute per account. In-process, like the rate
# limiter: a courtesy limit for a single instance, not a distributed control.
_COOLDOWN_SECONDS = 60.0
_last_sent: dict[str, float] = {}


class CooldownError(AppError):
    def __init__(self) -> None:
        super().__init__(
            "Wait a minute before requesting another email.",
            status_code=429,
            code="rate_limited",
        )


def missing_settings(settings: Settings) -> list[str]:
    """Which Brevo variables are absent, by name.

    Named rather than counted, because "not configured" sends whoever is
    deploying back to the documentation to work out which of the two it is.
    Variable names are not secrets; the values are, and those never appear.
    """
    missing = []
    if settings.brevo_api_key is None:
        missing.append("BREVO_API_KEY")
    if not settings.brevo_sender_email:
        missing.append("BREVO_SENDER_EMAIL")
    return missing


def is_configured(settings: Settings) -> bool:
    return not missing_settings(settings)


def check_cooldown(uid: str) -> None:
    previous = _last_sent.get(uid, 0.0)
    if time.monotonic() - previous < _COOLDOWN_SECONDS:
        raise CooldownError()


async def _deliver(payload: dict[str, Any], *, api_key: Any, failed: str) -> None:
    """Hand one message to Brevo.

    Extracted so a second kind of mail does not mean a second copy of the
    error handling — which is where the useful diagnosis of a 401 lives, and
    the last place worth having two of.
    """
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.post(
                _ENDPOINT,
                headers={
                    "api-key": api_key.get_secret_value(),
                    "content-type": "application/json",
                    "accept": "application/json",
                },
                json=payload,
            )
    except httpx.HTTPError as exc:
        logger.warning("Brevo unreachable: %s", type(exc).__name__)
        raise UpstreamError(failed) from exc

    if not response.is_success:
        # Brevo answers failures as {"code": ..., "message": ...}. Those two
        # fields are the diagnosis and neither carries the key, so they are
        # logged by name rather than the body being dumped whole — an earlier
        # version logged the status alone, which told nobody anything.
        detail: dict[str, Any] = {}
        try:
            parsed = response.json()
            if isinstance(parsed, dict):
                detail = parsed
        except ValueError:
            pass

        logger.error(
            "Brevo rejected the send: HTTP %s %s — %s",
            response.status_code,
            str(detail.get("code", "no-code"))[:60],
            str(detail.get("message", "no message"))[:300],
        )

        if response.status_code in (401, 403):
            # Almost always one of two things, and neither is obvious from the
            # status: a key that is wrong, or Brevo's "Authorised IPs" setting
            # refusing the call because the server's address is not on the list.
            logger.error(
                "Check the Brevo API key, and Brevo > Security > Authorised IPs "
                "— a restricted account refuses calls from unlisted servers."
            )

        raise UpstreamError("The email provider rejected the request.")



async def send_verification(
    *, uid: str, email: str, name: str, link: str, settings: Settings
) -> None:
    """Send the branded confirm-your-address email.

    Firebase's own template is deliberately not triggered: the link is fetched
    from Identity Toolkit and carried by this message, so exactly one email
    goes out rather than ours plus an unbranded duplicate.
    """
    # Bound to a local so the type narrows: missing_settings rules None out,
    # but only the explicit check tells the checker that.
    api_key = settings.brevo_api_key
    absent = missing_settings(settings)

    if absent or api_key is None:
        raise AppError(
            "Email sending is not configured on the server: "
            + ", ".join(absent)
            + " not set.",
            status_code=501,
            code="not_configured",
        )

    check_cooldown(uid)

    app_url = settings.app_url.rstrip("/")
    brand = settings.brevo_sender_name

    payload = {
        "sender": {"email": settings.brevo_sender_email, "name": brand},
        "to": [{"email": email, "name": name or email}],
        "subject": f"Confirm your email to open your {brand} journal",
        "htmlContent": verification_html(
            link=link, name=name, brand=brand, gif_url=f"{app_url}/email/verify.gif"
        ),
        # A text part materially improves deliverability.
        "textContent": verification_text(link=link, name=name, brand=brand),
        "tags": ["verification"],
    }

    await _deliver(
        payload,
        api_key=api_key,
        failed="Could not send the verification email.",
    )

    _last_sent[uid] = time.monotonic()


async def send_invitation(
    *,
    email: str,
    student_name: str,
    coach_name: str,
    note: str,
    settings_record: UniversitySettings,
    settings: Settings,
) -> None:
    """Send a coach's invitation to a trader.

    The recipient here *is* taken from a request, which is the one place this
    service does that — and it is why the route above it refuses an address
    with no account. An invitation can only reach somebody who already signed
    up, so this is not a way to aim mail from our domain at a stranger.

    No cooldown keyed on the coach: a coach enrolling a cohort sends several in
    a row legitimately. The route's standard rate limit is what bounds it.
    """
    api_key = settings.brevo_api_key
    absent = missing_settings(settings)

    if absent or api_key is None:
        raise AppError(
            "Email sending is not configured on the server: "
            + ", ".join(absent)
            + " not set.",
            status_code=501,
            code="not_configured",
        )

    brand = settings.brevo_sender_name
    university = settings_record.name.strip() or f"{coach_name}'s desk"
    subject = (
        settings_record.template.subject.strip()
        or f"{coach_name} invited you to {university}"
    )

    payload = {
        "sender": {"email": settings.brevo_sender_email, "name": brand},
        "to": [{"email": email, "name": student_name or email}],
        # The coach writes the subject; the sender name stays ours, so the
        # message can never look like it came from somewhere it did not.
        "subject": subject[:200],
        "htmlContent": invitation_html(
            settings=settings_record,
            coach_name=coach_name,
            student_name=student_name,
            note=note,
            app_url=settings.app_url,
            brand=brand,
        ),
        "textContent": invitation_text(
            settings=settings_record,
            coach_name=coach_name,
            student_name=student_name,
            note=note,
            app_url=settings.app_url,
        ),
        "tags": ["invitation"],
    }

    await _deliver(
        payload, api_key=api_key, failed="Could not send the invitation email."
    )
