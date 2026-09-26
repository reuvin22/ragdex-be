"""The invitation email, assembled.

A coach writes three fragments; this wraps them in a shell. The shell is
table-based with inline styles throughout, for the same reason the
verification mail is: it is the only thing Gmail, Outlook and Apple Mail all
render the same way.

The division of labour is the point. The coach owns the words; this file owns
the envelope — the outer table, the width, the accept button, and the line
saying who sent it. An author cannot remove the last of those, which is what
keeps an invitation from our domain honest about where it came from.

Everything interpolated is either already sanitised (the three fragments, by
``mail_html.sanitise`` on the way into storage) or escaped here. A display name
is user-supplied text going into HTML and is not trusted twice just because it
was trusted once.
"""

from __future__ import annotations

from datetime import UTC, datetime
from html import escape
from re import sub

from app.models.schemas.university import EmailTemplate, UniversitySettings
from app.views.mail_html import to_text

#: The accent a template falls back to. Matches the product's own.
DEFAULT_ACCENT = "#6353e8"

#: Only a hex colour is taken from the template. The value lands inside a
#: `style` attribute, so anything else is a way into the markup.
def _accent(value: str) -> str:
    candidate = value.strip()
    hexish = all(char in "0123456789abcdefABCDEF" for char in candidate[1:])

    if len(candidate) in (4, 7) and candidate.startswith("#") and hexish:
        return candidate

    return DEFAULT_ACCENT


def default_body(coach_name: str) -> str:
    """What is sent when a coach has not written anything.

    A real message rather than a placeholder. A coach who never opens the
    settings screen should still send something a stranger would answer.
    """
    return (
        f"<p>{escape(coach_name)} has invited you to join their trading "
        "program on RagDex.</p>"
        "<p>Accepting lets them read your journal so they can review your "
        "trades with you. It does not let them change anything, and you can "
        "end it whenever you like.</p>"
    )


#: Tokens an author may use in their template.
#:
#: Deliberately few and deliberately not a template language: this is a
#: find-and-replace over a sanitised string, so there is no expression to
#: evaluate and nothing an author can write that runs.
def _fill(markup: str, *, coach: str, student: str, note: str) -> str:
    replacements = {
        "{{coach}}": escape(coach),
        "{{student}}": escape(student),
        "{{note}}": escape(note),
    }

    filled = markup
    for token, value in replacements.items():
        filled = filled.replace(token, value)

    # Anything that still looks like a token was not one. Left as written
    # rather than stripped — an author who typed {{price}} meant to.
    return filled


def invitation_html(
    *,
    settings: UniversitySettings,
    coach_name: str,
    student_name: str,
    note: str,
    app_url: str,
    brand: str,
) -> str:
    template: EmailTemplate = settings.template
    accent = _accent(template.accent)
    year = datetime.now(UTC).year

    program = escape(settings.name.strip() or f"{coach_name}'s program")
    safe_brand = escape(brand)
    safe_link = escape(f"{app_url.rstrip('/')}/#/university", quote=True)

    body = template.body_html.strip() or default_body(coach_name)
    filled = {
        part: _fill(value, coach=coach_name, student=student_name, note=note)
        for part, value in (
            ("header", template.header_html),
            ("body", body),
            ("footer", template.footer_html),
        )
    }

    header_block = (
        f'<tr><td style="padding:0 0 22px 0">{filled["header"]}</td></tr>'
        if filled["header"].strip()
        else ""
    )

    note_block = (
        f'<tr><td style="padding:0 0 20px 0"><div style="border-left:3px solid '
        f'{accent};padding:2px 0 2px 14px;color:#4b5563;font-style:italic">'
        f"{escape(note)}</div></td></tr>"
        if note.strip()
        else ""
    )

    footer_block = (
        f'<tr><td style="padding:22px 0 0 0;border-top:1px solid #e5e7eb;'
        f'color:#6b7280;font-size:13px">{filled["footer"]}</td></tr>'
        if filled["footer"].strip()
        else ""
    )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<meta name="x-apple-disable-message-reformatting" />
<meta name="color-scheme" content="light" />
<meta name="supported-color-schemes" content="light" />
<title>{program}</title>
</head>
<body style="margin:0;padding:0;background:#f3f4f6">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#f3f4f6">
<tr><td align="center" style="padding:28px 12px">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" style="width:600px;max-width:100%;background:#ffffff;border-radius:12px;border:1px solid #e5e7eb">
<tr><td style="padding:32px 34px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;font-size:15px;line-height:1.62;color:#111827">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
{header_block}
<tr><td style="padding:0 0 18px 0">{filled["body"]}</td></tr>
{note_block}
<tr><td style="padding:6px 0 26px 0">
<a href="{safe_link}" target="_blank" rel="noopener noreferrer" style="display:inline-block;background:{accent};color:#ffffff;text-decoration:none;font-weight:600;font-size:15px;padding:13px 26px;border-radius:8px">Open RagDex to accept</a>
</td></tr>
{footer_block}
<tr><td style="padding:18px 0 0 0;color:#9ca3af;font-size:12px;line-height:1.55">
You are reading this because {escape(coach_name)} invited you to {program} on {safe_brand}. If you were not expecting it, you can ignore this message — nothing happens until you accept, and nobody can see your journal in the meantime.
</td></tr>
<tr><td style="padding:14px 0 0 0;color:#9ca3af;font-size:12px">&copy; {year} {safe_brand}</td></tr>
</table>
</td></tr>
</table>
</td></tr>
</table>
</body>
</html>"""


def invitation_text(
    *,
    settings: UniversitySettings,
    coach_name: str,
    student_name: str,
    note: str,
    app_url: str,
) -> str:
    """The plain-text half, built from the same fragments."""
    template = settings.template
    body = template.body_html.strip() or default_body(coach_name)

    def rendered(markup: str) -> str:
        return to_text(_fill(markup, coach=coach_name, student=student_name, note=note))

    parts = [
        rendered(template.header_html),
        rendered(body),
        f"“{note.strip()}”" if note.strip() else "",
        f"Open RagDex to accept: {app_url.rstrip('/')}/#/university",
        rendered(template.footer_html),
    ]

    joined = "\n\n".join(part for part in parts if part.strip())
    # Collapse the runs of blank lines that dropping an empty part leaves.
    return sub(r"\n{3,}", "\n\n", joined).strip()
