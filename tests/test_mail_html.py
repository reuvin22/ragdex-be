"""The sanitiser between a coach's editor and somebody else's inbox.

The threat is specific: this service sends mail from a domain recipients
trust, and a coach supplies the HTML. Every test below is a way that could go
wrong rather than a feature working.
"""

from __future__ import annotations

import pytest
from app.views.mail_html import sanitise, to_text


def test_ordinary_formatting_survives():
    markup = "<p>Hello <strong>there</strong>, and <em>welcome</em>.</p>"
    assert sanitise(markup) == markup


def test_a_script_tag_and_its_contents_are_dropped():
    out = sanitise("<p>before</p><script>alert(document.cookie)</script><p>after</p>")

    assert "<script" not in out
    # The body goes too: left behind it would show up as prose, and in a
    # client that re-parses it is not even inert.
    assert "alert" not in out
    assert "before" in out and "after" in out


def test_a_style_block_is_dropped_whole():
    out = sanitise("<style>body{display:none}</style><p>kept</p>")

    assert "display:none" not in out
    assert "kept" in out


def test_an_event_handler_attribute_does_not_survive():
    out = sanitise('<p onclick="steal()">text</p>')

    assert "onclick" not in out
    assert "steal" not in out
    assert "text" in out


@pytest.mark.parametrize(
    "href",
    [
        "javascript:alert(1)",
        "JaVaScRiPt:alert(1)",
        "data:text/html;base64,PHNjcmlwdD4=",
        "vbscript:msgbox",
        "file:///etc/passwd",
    ],
)
def test_only_http_links_survive(href):
    out = sanitise(f'<a href="{href}">click me</a>')

    assert "href" not in out
    # The words stay. A refused target should not silently delete what
    # somebody wrote around it.
    assert "click me" in out


def test_an_https_link_is_kept_and_made_safe():
    out = sanitise('<a href="https://example.com">go</a>')

    assert 'href="https://example.com"' in out
    assert 'rel="noopener noreferrer"' in out
    assert 'target="_blank"' in out


def test_an_image_from_the_web_is_kept():
    out = sanitise('<img src="https://cdn.example.com/a.png" alt="A chart" />')

    assert "https://cdn.example.com/a.png" in out
    assert 'alt="A chart"' in out


def test_a_data_uri_image_is_refused():
    out = sanitise('<img src="data:image/png;base64,iVBORw0KGgo=" />')

    assert "base64" not in out


def test_style_is_reduced_to_an_allowlist():
    out = sanitise(
        '<p style="color:#ff0000;position:fixed;background-image:url(http://x/y)">hi</p>'
    )

    assert "color:#ff0000" in out
    assert "position" not in out
    assert "background-image" not in out


def test_a_style_value_with_a_function_call_is_refused():
    out = sanitise('<p style="color:expression(alert(1))">hi</p>')

    assert "expression" not in out


def test_an_unknown_tag_is_unwrapped_not_deleted():
    out = sanitise("<marquee>still readable</marquee>")

    assert "marquee" not in out
    assert "still readable" in out


def test_text_is_escaped():
    out = sanitise("<p>5 > 3 & 2 < 4</p>")

    assert "&gt;" in out and "&lt;" in out and "&amp;" in out


def test_a_stray_closing_tag_cannot_escape_the_fragment():
    """The fragment is wrapped in a table shell. A `</td>` in the middle of it
    must not be able to close that shell early."""
    out = sanitise("</td></table><p>after</p>")

    assert "</td>" not in out
    assert "</table>" not in out


def test_unclosed_tags_are_closed():
    out = sanitise("<div><p>dangling")

    assert out.endswith("</p></div>")


def test_an_iframe_is_dropped_with_its_contents():
    out = sanitise('<iframe src="https://evil.example">fallback</iframe><p>ok</p>')

    assert "iframe" not in out
    assert "evil.example" not in out
    assert "ok" in out


def test_a_very_long_template_is_truncated_rather_than_refused():
    out = sanitise("<p>" + ("a" * 50_000) + "</p>")

    assert len(out) < 25_000


def test_to_text_gives_a_readable_fallback():
    text = to_text("<h2>Welcome</h2><p>Line one.</p><p>Line two.</p>")

    assert text == "Welcome\nLine one.\nLine two."


def test_to_text_drops_markup_entirely():
    assert "<" not in to_text('<p><a href="https://x.com">link</a></p>')
