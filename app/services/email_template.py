"""Verification email markup.

A direct port of the Vercel function's template, which this service replaces.

Email clients are a hostile rendering target: no external stylesheets, no
flexbox or grid in Outlook, and inconsistent support for anything modern. So
this is table-based with inline styles throughout, which is verbose but the
only thing that renders the same in Gmail, Outlook and Apple Mail.

Every interpolated value is escaped. A display name is user-supplied text going
into HTML, and the link is built from configuration — neither is trusted here
just because it was trusted upstream.
"""

from __future__ import annotations

from datetime import UTC, datetime
from html import escape


def _greeting(name: str) -> str:
    return f"Hi {escape(name)}," if name else "Hi there,"


def verification_html(*, link: str, name: str, brand: str, gif_url: str) -> str:
    greeting = _greeting(name)
    safe_brand = escape(brand)
    safe_link = escape(link, quote=True)
    safe_gif = escape(gif_url, quote=True)
    year = datetime.now(UTC).year

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<meta name="x-apple-disable-message-reformatting" />
<meta name="color-scheme" content="light" />
<meta name="supported-color-schemes" content="light" />
<title>Confirm your {safe_brand} email</title>
<!--[if mso]>
<style>body,table,td,a{{font-family:Arial,Helvetica,sans-serif !important}}</style>
<![endif]-->
</head>
<body style="margin:0;padding:0;background-color:#eef0f6;">

<!-- Inbox preview line; hidden in the body itself. -->
<div style="display:none;max-height:0;overflow:hidden;opacity:0;">
  Confirm your address to unlock your {safe_brand} trading journal.
</div>

<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#eef0f6;">
<tr>
<td align="center" style="padding:32px 16px;">

  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="max-width:560px;background-color:#ffffff;border-radius:16px;overflow:hidden;box-shadow:0 2px 8px rgba(24,20,60,0.07);">

    <!-- Header -->
    <tr>
    <td style="background-color:#15142b;padding:26px 32px;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
      <tr>
        <td align="left" style="font-family:'Segoe UI',Helvetica,Arial,sans-serif;font-size:22px;font-weight:600;color:#ffffff;letter-spacing:-0.4px;">
          {safe_brand}
        </td>
        <td align="right" style="font-family:'Segoe UI',Helvetica,Arial,sans-serif;font-size:10px;font-weight:600;letter-spacing:1.6px;text-transform:uppercase;color:#8f83ff;">
          Account Security
        </td>
      </tr>
      </table>
    </td>
    </tr>

    <!-- Body -->
    <tr>
    <td style="padding:34px 32px 8px;font-family:'Segoe UI',Helvetica,Arial,sans-serif;">
      <h1 style="margin:0 0 14px;font-size:23px;line-height:1.3;font-weight:600;color:#15142b;letter-spacing:-0.4px;">
        Confirm your email address
      </h1>
      <p style="margin:0 0 12px;font-size:15px;line-height:1.65;color:#4f4a72;">
        {greeting}
      </p>
      <p style="margin:0 0 26px;font-size:15px;line-height:1.65;color:#4f4a72;">
        You're one step from your journal. Confirm this address and every trade you
        log will sync privately to your account.
      </p>

      <!-- CTA -->
      <table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin:0 0 22px;">
      <tr>
      <td align="center" bgcolor="#6353e8" style="border-radius:10px;">
        <a href="{safe_link}" target="_blank" style="display:inline-block;padding:14px 34px;font-family:'Segoe UI',Helvetica,Arial,sans-serif;font-size:15px;font-weight:600;color:#ffffff;text-decoration:none;border-radius:10px;">
          Confirm my email
        </a>
      </td>
      </tr>
      </table>

      <p style="margin:0 0 30px;font-size:13px;line-height:1.6;color:#74708f;">
        This link expires in one hour and can be used once.
      </p>
    </td>
    </tr>

    <!-- Animation -->
    <tr>
    <td style="padding:0 32px 8px;">
      <img src="{safe_gif}" width="496" alt="An account equity curve climbing across ninety days of logged trades"
           style="display:block;width:100%;max-width:496px;height:auto;border:0;border-radius:12px;outline:none;text-decoration:none;" />
      <p style="margin:12px 0 0;font-family:'Segoe UI',Helvetica,Arial,sans-serif;font-size:12.5px;line-height:1.6;color:#74708f;text-align:center;">
        Every entry, exit and mistake &mdash; measured, not remembered.
      </p>
    </td>
    </tr>

    <!-- Fallback link -->
    <tr>
    <td style="padding:26px 32px 0;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#f5f4fb;border-radius:10px;">
      <tr>
      <td style="padding:14px 16px;font-family:'Segoe UI',Helvetica,Arial,sans-serif;">
        <p style="margin:0 0 6px;font-size:12px;font-weight:600;color:#4f4a72;">
          Button not working?
        </p>
        <p style="margin:0;font-size:12px;line-height:1.6;color:#74708f;word-break:break-all;">
          <a href="{safe_link}" target="_blank" style="color:#6353e8;text-decoration:underline;">{safe_link}</a>
        </p>
      </td>
      </tr>
      </table>
    </td>
    </tr>

    <!-- Footer -->
    <tr>
    <td style="padding:26px 32px 30px;font-family:'Segoe UI',Helvetica,Arial,sans-serif;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
      <tr><td style="border-top:1px solid #e6e4f0;padding-top:20px;">
        <p style="margin:0 0 8px;font-size:12.5px;line-height:1.65;color:#74708f;">
          Didn't create a {safe_brand} account? You can safely ignore this email &mdash;
          nothing will be activated without this confirmation.
        </p>
        <p style="margin:0;font-size:12px;line-height:1.65;color:#9793ad;">
          &copy; {year} {safe_brand} &middot; Know Your Trades. Grow Your Edge.
        </p>
      </td></tr>
      </table>
    </td>
    </tr>

  </table>

</td>
</tr>
</table>
</body>
</html>"""


def verification_text(*, link: str, name: str, brand: str) -> str:
    """Plain-text alternative. Its absence is a real spam-score penalty."""
    greeting = f"Hi {name}," if name else "Hi there,"

    return f"""{greeting}

Confirm your email address to open your {brand} trading journal.

{link}

This link expires in one hour and can be used once.

Didn't create a {brand} account? Ignore this email and nothing will be activated.

— {brand}
Know Your Trades. Grow Your Edge."""
