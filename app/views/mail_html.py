"""Sanitising coach-written HTML before it is emailed.

A coach composes an invitation in a rich text editor and this service sends it,
from our domain, to an address they chose. That is a combination worth being
blunt about: without this module, the product is a way to send arbitrary HTML
from a domain other people trust. The editor being ours is no defence — the
API takes a string, and a string can be posted by anything.

So the rule here is allowlist, not blocklist. Anything not named below is
dropped, including the tag, its attributes, and — for ``script`` and ``style``
— everything between its tags. Attempting to enumerate what is dangerous is
how sanitisers get bypassed; enumerating what is permitted is how they do not.

Written against ``html.parser`` from the standard library rather than pulling
in a sanitiser, for the same reason ``r2.py`` signs with ``hmac`` rather than
importing an S3 SDK: the job is small, the dependency is not, and the rules
being visible here is worth more than the rules being someone else's.
"""

from __future__ import annotations

import re
from html import escape
from html.parser import HTMLParser

#: Tags a mail client will render and an editor can produce.
#:
#: No ``table``. Real email layout is table-based and this deliberately is not:
#: the template is wrapped by a table-based shell this service owns (see
#: ``invitation_html``), and letting an author nest their own tables inside it
#: is how a template breaks Outlook in a way nobody can debug from here.
ALLOWED_TAGS = frozenset(
    {
        "p", "br", "div", "span", "hr",
        "strong", "b", "em", "i", "u", "s", "mark",
        "h1", "h2", "h3", "h4",
        "ul", "ol", "li",
        "blockquote", "a", "img",
    }
)

#: Tags that close themselves. Emitted with a trailing slash for XHTML-ish
#: clients that still care.
VOID_TAGS = frozenset({"br", "hr", "img"})

#: Tags whose *contents* go too, not just the tag.
#:
#: Dropping `<script>` but keeping its text would put the script body into the
#: message as visible prose, which is both ugly and, in a few clients that
#: re-parse, not even safe.
DROP_CONTENT = frozenset({"script", "style", "iframe", "object", "embed", "template"})

ALLOWED_ATTRS: dict[str, frozenset[str]] = {
    "a": frozenset({"href", "title"}),
    "img": frozenset({"src", "alt", "width", "height"}),
}

#: Style properties an author may set, and nothing else.
#:
#: No ``position``, no ``background-image``, no ``content`` — none of which a
#: mail client honours reliably anyway, and all of which are ways to fetch or
#: overlay something the author did not write in plain sight.
ALLOWED_STYLES = frozenset(
    {
        "color", "background-color", "font-size", "font-weight", "font-style",
        "text-align", "text-decoration", "line-height", "margin", "padding",
    }
)

#: A style value has to look like a colour, a length, or a plain keyword.
#: Anything with a bracket, a backslash or a colon in it is refused rather
#: than unpicked — `url(...)`, `expression(...)` and escaped payloads all die
#: on the same rule.
_SAFE_STYLE_VALUE = re.compile(r"^[#\w\s.,%-]+$")

#: Links and images may only point at the open web.
#:
#: `javascript:` is the obvious one. `data:` is the less obvious one and is
#: refused too: a data URI in a mail client is a payload nobody can inspect
#: from a sent-items list, and images have a home in R2 already.
_SAFE_URL = re.compile(r"^https?://", re.IGNORECASE)

#: How much markup one template section may carry.
MAX_SECTION = 20_000


def _clean_style(value: str) -> str:
    kept: list[str] = []

    for declaration in value.split(";"):
        name, sep, setting = declaration.partition(":")
        if not sep:
            continue

        prop = name.strip().lower()
        val = setting.strip()

        if prop in ALLOWED_STYLES and val and _SAFE_STYLE_VALUE.match(val):
            kept.append(f"{prop}:{val}")

    return ";".join(kept)


class _Sanitiser(HTMLParser):
    """Rebuilds the document from only what is permitted."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self._open: list[str] = []
        #: Depth inside a tag whose contents are being discarded.
        self._muted = 0

    # -- helpers ---------------------------------------------------------

    def _attrs(self, tag: str, attrs: list[tuple[str, str | None]]) -> str:
        permitted = ALLOWED_ATTRS.get(tag, frozenset())
        rendered: list[str] = []

        for raw_name, raw_value in attrs:
            name = raw_name.lower()
            value = raw_value or ""

            if name == "style":
                cleaned = _clean_style(value)
                if cleaned:
                    rendered.append(f'style="{escape(cleaned, quote=True)}"')
                continue

            if name not in permitted:
                continue

            if name in ("href", "src") and not _SAFE_URL.match(value.strip()):
                # The attribute goes, but the tag stays — a link whose target
                # was refused becomes plain text rather than vanishing along
                # with the words somebody wrote.
                continue

            if name in ("width", "height") and not value.strip().isdigit():
                continue

            rendered.append(f'{name}="{escape(value, quote=True)}"')

        # Every surviving link leaves our domain and opens in a mail client.
        if tag == "a":
            rendered.append('target="_blank"')
            rendered.append('rel="noopener noreferrer"')

        return (" " + " ".join(rendered)) if rendered else ""

    # -- parser hooks ----------------------------------------------------

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._muted:
            return

        if tag in DROP_CONTENT:
            self._muted = 1
            return

        if tag not in ALLOWED_TAGS:
            return

        if tag in VOID_TAGS:
            self.out.append(f"<{tag}{self._attrs(tag, attrs)} />")
            return

        self.out.append(f"<{tag}{self._attrs(tag, attrs)}>")
        self._open.append(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._muted or tag not in ALLOWED_TAGS:
            return
        self.out.append(f"<{tag}{self._attrs(tag, attrs)} />")

    def handle_endtag(self, tag: str) -> None:
        if self._muted:
            if tag in DROP_CONTENT:
                self._muted = 0
            return

        if tag in VOID_TAGS or tag not in ALLOWED_TAGS:
            return

        # Only close something actually open, so a stray `</div>` cannot close
        # the shell this fragment is about to be wrapped in.
        if tag in self._open:
            while self._open:
                current = self._open.pop()
                self.out.append(f"</{current}>")
                if current == tag:
                    break

    def handle_data(self, data: str) -> None:
        if not self._muted:
            self.out.append(escape(data))

    def close(self) -> str:  # type: ignore[override]
        super().close()
        while self._open:
            self.out.append(f"</{self._open.pop()}>")
        return "".join(self.out)


def sanitise(markup: str) -> str:
    """One template section, reduced to what may be sent.

    Never raises on bad input. A template somebody pasted from a word
    processor is mostly tags this does not know, and the useful answer is the
    text with the junk removed rather than a rejection they cannot act on.
    """
    parser = _Sanitiser()
    parser.feed(markup[:MAX_SECTION])
    return parser.close()


def to_text(markup: str) -> str:
    """A plain-text fallback, for clients that will not render HTML.

    Every message needs one. Without it a mail is more likely to be filtered,
    and the reader with images off sees nothing at all.
    """
    without_breaks = re.sub(r"<(br|/p|/div|/h[1-4]|/li)[^>]*>", "\n", markup, flags=re.I)
    stripped = re.sub(r"<[^>]+>", "", without_breaks)

    lines = [line.strip() for line in stripped.splitlines()]
    return "\n".join(line for line in lines if line)
