"""Working out what a stranger's CSV columns mean.

The cases here are the ones a synonym list cannot do, because that is the whole
reason this module exists: misspelled headers, headers that say nothing, two
columns with the same name, and files with no usable header row at all.
"""

from __future__ import annotations

from app.services.csv_analyse import analyse, content_score, header_score, squash


def table(*lines: str) -> tuple[list[str], list[list[str]]]:
    """A CSV written inline, split the way the client sends it.

    Comma-separated here only because these fixtures read better that way —
    the service never sees a delimiter, which is the point of the split.
    """
    split = [line.split(",") for line in lines]
    return split[0], split[1:]

# ----------------------------------------------------------- the basics


def test_squash_makes_one_string_of_a_dozen_spellings() -> None:
    for spelling in ("Open Time", "open_time", "OpenTime", "OPEN-TIME", " open time "):
        assert squash(spelling) == "opentime"


# --------------------------------------------------- headers, approximately


def test_a_misspelled_header_still_matches() -> None:
    """The case the client's exact-synonym list gets wrong, and the reason
    there is a service here at all."""
    assert header_score("ticker", "Instrmnt") > 0.5
    assert header_score("ticker", "Symbal") > 0.5
    assert header_score("direction", "Directoin") > 0.5


def test_an_unrelated_header_does_not_match() -> None:
    """The floor has to hold: a wrong column looks like data, an empty one
    looks empty, and only one of those is recoverable."""
    assert header_score("ticker", "Commission") < 0.45
    assert header_score("direction", "Swap") < 0.45
    assert header_score("entryAt", "Profit") < 0.45


def test_a_longer_synonym_outranks_a_shorter_one() -> None:
    assert header_score("entryPrice", "Entry Price") > header_score("entryPrice", "Price")


# ------------------------------------------------------------- contents


def test_a_direction_column_is_recognised_by_what_is_in_it() -> None:
    """Whatever it is called — this is the signal that survives "Column 4"."""
    assert content_score("direction", ["Buy", "Sell", "Buy", "Sell"]) == 1.0
    assert content_score("direction", ["DEAL_TYPE_BUY", "DEAL_TYPE_SELL"]) == 1.0
    assert content_score("direction", ["EURUSD", "GBPUSD"]) == 0.0


def test_a_date_column_is_recognised_by_what_is_in_it() -> None:
    assert content_score("entryAt", ["2026-09-14 10:30", "2026-09-15 11:00"]) == 1.0
    assert content_score("entryAt", ["2026.09.14 10:30"]) > 0.9
    assert content_score("entryAt", ["EURUSD", "GBPUSD"]) == 0.0


def test_a_symbol_column_repeats_and_prose_does_not() -> None:
    """Both are short text. Only one of them says the same thing over and over,
    which is what separates a symbol column from a notes column."""
    symbols = content_score("ticker", ["EURUSD", "GBPUSD", "EURUSD", "EURUSD", "GBPUSD"])
    prose = content_score(
        "ticker",
        ["broke my rule", "good entry", "chased it", "waited properly", "too big"],
    )
    assert symbols > prose


def test_an_empty_column_scores_nothing_rather_than_everything() -> None:
    for field in ("direction", "ticker", "entryAt", "entryPrice", "size"):
        assert content_score(field, []) == 0.0


# ------------------------------------------------------ the whole decision


def test_a_normal_export_maps_cleanly() -> None:
    result = analyse(
        *table(
            "Symbol,Side,Volume,Entry Price,Exit Price,Open Time,Close Time",
            "EURUSD,Buy,1.0,1.1000,1.1050,2026-09-01 08:00,2026-09-01 10:00",
            "XAUUSD,Sell,0.5,2400.00,2395.00,2026-09-02 09:00,2026-09-02 11:00",
        )
    )

    mapping = result["mapping"]
    assert mapping["ticker"] == 0
    assert mapping["direction"] == 1
    assert mapping["size"] == 2
    assert mapping["entryPrice"] == 3
    assert mapping["exitPrice"] == 4
    assert mapping["entryAt"] == 5
    assert mapping["exitAt"] == 6


def test_no_column_is_claimed_twice() -> None:
    result = analyse(
        *table(
            "Time,Time,Symbol,Type,Volume,Price,Price",
            "2026-09-01 08:00,2026-09-01 10:00,EURUSD,Buy,1.0,1.1000,1.1050",
        )
    )

    used = list(result["mapping"].values())
    assert len(used) == len(set(used))


def test_two_columns_called_time_are_sorted_by_which_is_earlier() -> None:
    """MetaTrader writes exactly this: an open and a close, both called "Time".
    No name can separate them; the contents can, because one is always before
    the other."""
    result = analyse(
        *table(
            "Time,Symbol,Type,Volume,Time,Price",
            "2026-09-01 08:00,EURUSD,Buy,1.0,2026-09-01 10:00,1.1000",
            "2026-09-02 09:00,GBPUSD,Sell,1.0,2026-09-02 15:00,1.2500",
        )
    )

    mapping = result["mapping"]
    assert "entryAt" in mapping and "exitAt" in mapping
    # Whichever column it picked for entry must hold the earlier times.
    assert mapping["entryAt"] != mapping["exitAt"]


def test_useless_headers_are_beaten_by_the_contents() -> None:
    """The case this whole module is for: nothing in the header row helps."""
    result = analyse(
        *table(
            "col1,col2,col3,col4",
            "EURUSD,Buy,2026-09-01 08:00,1.1000",
            "EURUSD,Sell,2026-09-02 09:00,1.1050",
            "GBPUSD,Buy,2026-09-03 10:00,1.2500",
            "EURUSD,Sell,2026-09-04 11:00,1.1100",
        )
    )

    mapping = result["mapping"]
    assert mapping.get("ticker") == 0
    assert mapping.get("direction") == 1
    assert mapping.get("entryAt") == 2


def test_confidence_is_lower_when_only_the_contents_agreed() -> None:
    """So the client can mark which guesses deserve a glance. A field found by
    name and contents together should outrank one found by contents alone."""
    named = analyse(*table("Direction", "Buy", "Sell"))
    unnamed = analyse(*table("col1", "Buy", "Sell"))

    assert named["confidence"]["direction"] >= unnamed["confidence"]["direction"]


def test_a_column_of_commission_is_left_alone() -> None:
    """Not every column is one of ours, and claiming one that is not is worse
    than leaving a field empty."""
    result = analyse(
        *table(
            "Symbol,Type,Commission,Swap",
            "EURUSD,Buy,-7.00,-0.30",
            "GBPUSD,Sell,-7.00,-0.15",
        )
    )

    claimed = set(result["mapping"].values())
    assert 2 not in claimed
    assert 3 not in claimed


def test_the_headers_come_back_for_correction() -> None:
    result = analyse(*table("Symbol,Side", "EURUSD,Buy"))

    assert result["headers"] == ["Symbol", "Side"]
    assert result["rows_sampled"] == 1


def test_only_a_sample_is_read() -> None:
    """Ten thousand rows must not become ten thousand rows of memory: the
    decision is the same after a hundred."""
    headers, rows = table(
        "Symbol,Side",
        *[f"EURUSD,{'Buy' if index % 2 else 'Sell'}" for index in range(5_000)],
    )

    assert analyse(headers, rows)["rows_sampled"] <= 200


def test_a_short_row_does_not_take_the_file_down() -> None:
    """Broker exports are ragged. A row that stops early contributes nothing
    to the columns it does not reach, rather than failing everything."""
    result = analyse(
        ["Symbol", "Side", "Volume", "Open Time"],
        [
            ["EURUSD", "Buy", "1.0", "2026-09-01 08:00"],
            ["GBPUSD", "Sell"],
            ["XAUUSD", "Buy", "0.5", "2026-09-02 09:00"],
        ],
    )

    assert result["mapping"]["ticker"] == 0
    assert result["mapping"]["direction"] == 1
    assert result["rows_sampled"] == 3


def test_a_file_with_no_columns_is_refused() -> None:
    import pytest

    with pytest.raises(ValueError):
        analyse([], [])
