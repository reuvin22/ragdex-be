"""The behavioural leak card holds model-written text in a 320px rail.

Every field here is written by a model that is told to be brief and regularly
is not, so the limits are load-bearing rather than decorative. These cover the
trimming; the wrapping that stops a long value running out of the card is in
trades/src/components/ui.ts.
"""

from __future__ import annotations

from app.services.insights import _trim


def test_short_text_is_left_alone() -> None:
    assert _trim("Revenge trading", 80) == "Revenge trading"


def test_a_long_field_is_cut_on_a_word_boundary() -> None:
    """A hard slice at the character count ends mid-word, which reads as a
    rendering fault rather than as a summary that ran long."""
    trimmed = _trim("They broke their rules on fifteen of twenty trades", 30)

    assert trimmed.endswith("…")
    assert len(trimmed) <= 30
    # Cut between words, so no severed fragment is left behind.
    assert trimmed.replace("…", "").strip().split()[-1] in {
        "broke",
        "their",
        "rules",
        "on",
        "fifteen",
        "of",
    }


def test_trailing_punctuation_does_not_precede_the_ellipsis() -> None:
    assert "..…" not in _trim("One sentence, then another one here", 16)
    assert ",…" not in _trim("One sentence, then another one here", 16)


def test_one_unbroken_run_still_gets_cut() -> None:
    """No word boundary to fall back to, so the hard cut is all there is."""
    trimmed = _trim("x" * 200, 40)

    assert len(trimmed) == 40
    assert trimmed.endswith("…")


def test_missing_and_non_string_values_are_safe() -> None:
    assert _trim(None, 80) == ""
    assert _trim("", 80) == ""
    assert _trim(1234, 80) == "1234"
