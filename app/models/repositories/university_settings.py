"""A coach's programme and the invitation they wrote.

One document per coach. What is stored has already been through
``views.mail_html.sanitise`` — the cleaning happens on the way in, not on the
way out, so nothing can be sent that was not also what the editor showed back.
That ordering matters: a store that held raw markup and cleaned it at send
time would be one forgotten call away from mailing whatever was pasted in.
"""

from __future__ import annotations

from typing import Any

from google.cloud.firestore_v1 import SERVER_TIMESTAMP

from app.db.firestore import university_doc
from app.models.schemas.university import EmailTemplate, UniversitySettings
from app.views.mail_html import sanitise


def _to_settings(data: dict[str, Any]) -> UniversitySettings:
    raw = data.get("template")
    template = raw if isinstance(raw, dict) else {}

    return UniversitySettings(
        name=str(data.get("name", "")),
        blurb=str(data.get("blurb", "")),
        template=EmailTemplate(
            subject=str(template.get("subject", "")),
            header_html=str(template.get("headerHtml", "")),
            body_html=str(template.get("bodyHtml", "")),
            footer_html=str(template.get("footerHtml", "")),
            accent=str(template.get("accent", "")),
        ),
    )


def get_settings(coach_uid: str) -> UniversitySettings:
    """The programme, or an empty one.

    Empty rather than a 404: a coach who has never opened the settings screen
    still has a programme, it just has nothing written in it yet, and the send
    path falls back to a default template for exactly that case.
    """
    snapshot = university_doc(coach_uid).get()
    if not snapshot.exists:
        return UniversitySettings()

    return _to_settings(snapshot.to_dict() or {})


def save_settings(coach_uid: str, payload: UniversitySettings) -> UniversitySettings:
    """Store it, cleaned.

    The three markup sections are sanitised here and only here, so every path
    that writes a template goes through the same allowlist.
    """
    cleaned = UniversitySettings(
        name=payload.name.strip(),
        blurb=payload.blurb.strip(),
        template=EmailTemplate(
            subject=payload.template.subject.strip(),
            header_html=sanitise(payload.template.header_html),
            body_html=sanitise(payload.template.body_html),
            footer_html=sanitise(payload.template.footer_html),
            accent=payload.template.accent.strip(),
        ),
    )

    university_doc(coach_uid).set(
        {
            "name": cleaned.name,
            "blurb": cleaned.blurb,
            "template": {
                "subject": cleaned.template.subject,
                "headerHtml": cleaned.template.header_html,
                "bodyHtml": cleaned.template.body_html,
                "footerHtml": cleaned.template.footer_html,
                "accent": cleaned.template.accent,
            },
            "updatedAt": SERVER_TIMESTAMP,
        },
        merge=True,
    )

    return cleaned
