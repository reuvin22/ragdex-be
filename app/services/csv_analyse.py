"""Working out what somebody else's CSV columns mean.

The client can already read a CSV and convert its values; what it cannot do
well is decide *which column is which* when the headers do not match anything
it knows. "Instrmnt", "Px Open", "B/S", or a file exported with no useful
header row at all — a synonym list gets none of those.

So this looks at the data as well as the names, which is the part that works
when the names do not. Three signals, combined per column and field:

**The header, exactly.** A known synonym is the strongest evidence there is:
whoever wrote the exporter said what the column was.

**The header, approximately.** "Instrmnt" is one edit from "instrument" and
"Symbal" one from "symbol". Typos and truncations are common in files that
have been through a spreadsheet.

**The contents.** A column where nine values in ten are "buy" or "sell" is the
direction column whatever it is called. This is the signal that survives a
header of "col4", and it is the reason this module exists.

It is handed rows that are already split into cells, not a file. The client
detects the delimiter on its way in and its detector is tested — including the
case `csv.Sniffer` gets wrong, a semicolon file full of prose commas. Parsing
here as well would be a second answer to a settled question, and the two would
eventually disagree.

That leaves scoring, which is plain Python, and reading dates, which is not:
`dateutil` parses the dozen shapes a broker might write without being told
which. Those two are the whole of this file's needs, which is why pandas and
numpy are not among them — ninety megabytes of dataframe, on an instance with
five hundred, to take a median.
"""

from __future__ import annotations

import logging
from datetime import datetime
from difflib import SequenceMatcher
from statistics import median_low
from typing import Any, Callable

from dateutil import parser as dateparser

logger = logging.getLogger(__name__)

#: Fields the client can fill. Must match FIELDS in trades/src/lib/csvFormat.ts.
FIELDS = (
    "ticker",
    "direction",
    "size",
    "sizeUnit",
    "entryPrice",
    "exitPrice",
    "entryAt",
    "exitAt",
    "stopLoss",
    "takeProfit",
    "setup",
    "rationale",
    "notes",
)

#: Header names seen in real exports. Longer entries are stronger evidence, so
#: "entryprice" outranks a bare "price" on a file that has both.
SYNONYMS: dict[str, tuple[str, ...]] = {
    "ticker": ("symbol", "ticker", "instrument", "pair", "market", "asset", "contract"),
    # No "position": MetaTrader's Position column holds the position id, and it
    # otherwise claims the direction column away from "Type".
    "direction": ("direction", "side", "type", "ordertype", "buysell", "action"),
    "size": ("volume", "size", "lots", "lot", "quantity", "qty", "units", "shares"),
    "sizeUnit": ("unit", "sizeunit", "volumeunit"),
    "entryPrice": (
        "entryprice",
        "openprice",
        "priceopen",
        "fillprice",
        "avgprice",
        "entry",
        "open",
        # Last, deliberately: MetaTrader calls the opening price plainly
        # "Price" and the closing one "Close Price". Any longer synonym
        # outranks this, so it can only claim a column nothing else wanted.
        "price",
    ),
    "exitPrice": ("exitprice", "closeprice", "priceclose", "exit", "close"),
    "entryAt": (
        "entrytime",
        "opentime",
        "timeopen",
        "openedat",
        "entrydate",
        "opendate",
        "datetime",
        "time",
        "date",
        "opened",
    ),
    "exitAt": ("exittime", "closetime", "timeclose", "closedat", "exitdate", "closedate", "closed"),
    "stopLoss": ("stoploss", "sl", "stop", "slprice"),
    "takeProfit": ("takeprofit", "tp", "target", "tpprice"),
    "setup": ("setup", "strategy", "playbook", "system", "model"),
    "rationale": ("rationale", "reason", "thesis", "idea", "why"),
    "notes": ("notes", "comment", "comments", "note", "remark", "description"),
}

#: What a direction column says, in the exports seen so far.
LONG_WORDS = {"buy", "long", "b", "bot", "bought", "dealtypebuy", "positiontypebuy", "0"}
SHORT_WORDS = {"sell", "short", "s", "sld", "sold", "dealtypesell", "positiontypesell", "1"}

#: Below this, a near-miss on a header is a coincidence rather than a typo.
FUZZY_FLOOR = 0.82

#: Below this, nothing is claimed.
#:
#: A wrong column is worse than an empty one: an empty field is visible in the
#: preview and obviously needs attention, while a wrong one looks like data.
ACCEPT = 0.45

#: How many rows are scored, however many arrive.
MAX_ROWS = 200


def squash(value: object) -> str:
    """Lowercase, letters and digits only.

    So "Open Time", "open_time" and "OpenTime" are one string rather than
    three, and a stray BOM or non-breaking space stops mattering.
    """
    return "".join(character for character in str(value).lower() if character.isalnum())


def _fraction(values: list[str], test: Callable[[str], bool]) -> float:
    """What fraction of the values satisfy a test. Zero for an empty column."""
    if not values:
        return 0.0
    return sum(1 for value in values if test(value)) / len(values)


def _digits_only(value: str) -> str:
    return "".join(
        character for character in value if character.isdigit() or character in ".,-+"
    )


def _as_float(value: str) -> float | None:
    """A number in either decimal convention, or None.

    Both readings are tried because which one a file uses is not this module's
    question — the client settles it over the whole column. Here it only
    matters whether the value *is* a number.
    """
    text = _digits_only(value)
    if not any(character.isdigit() for character in text):
        return None

    for candidate in (text.replace(",", ""), text.replace(".", "").replace(",", ".")):
        try:
            return float(candidate)
        except ValueError:
            continue

    return None


def _numeric(value: str) -> bool:
    return _as_float(value) is not None


def _as_date(value: str) -> datetime | None:
    """A timestamp, whatever shape it was written in.

    `dayfirst=True` because most of the world and every MetaTrader export is,
    but the ambiguity does not matter here: this only decides whether a column
    *is* dates. Which way round day and month go is settled separately, over
    the whole column, by the client that already does it.
    """
    text = value.strip()
    # dateutil will happily read "1" as today at one o'clock, and a column of
    # small integers is a size, not a schedule.
    if len(text) < 6:
        return None

    try:
        return dateparser.parse(text, dayfirst=True, fuzzy=False)
    except (ValueError, OverflowError, TypeError):
        return None


def _datey(value: str) -> bool:
    return _as_date(value) is not None


def _looks_like_ticker(values: list[str]) -> float:
    """Short, mostly letters, and repeating — the shape of a symbol column."""
    if not values:
        return 0.0

    shaped = _fraction(
        values,
        lambda value: 1 <= len(value) <= 14
        and not _numeric(value)
        and sum(character.isalpha() for character in value) >= len(value) / 2,
    )

    # A symbol column repeats: a trader holds a handful of instruments. A notes
    # column is also short text, and it does not repeat — which is the only
    # thing separating the two by content.
    repetition = 1.0 - (len(set(values)) / len(values))

    return shaped * (0.6 + 0.4 * min(repetition * 2, 1.0))


def content_score(field: str, values: list[str]) -> float:
    """How much the *contents* of a column look like a given field.

    The signal that survives an unrecognisable header. Prices are the honest
    gap: an entry price and an exit price are indistinguishable by content, so
    both score alike and the header decides which is which.
    """
    if not values:
        return 0.0

    if field == "direction":
        return _fraction(values, lambda value: squash(value) in LONG_WORDS | SHORT_WORDS)

    if field in ("entryAt", "exitAt"):
        return _fraction(values, _datey)

    if field == "ticker":
        return _looks_like_ticker(values)

    if field in ("entryPrice", "exitPrice", "stopLoss", "takeProfit"):
        numeric = _fraction(values, _numeric)
        # Prices are positive and rarely whole; a column of small integers is a
        # size or a count, not a price.
        fractional = _fraction(
            values,
            lambda value: (_as_float(value) or 0) > 0 and (_as_float(value) or 0) % 1 != 0,
        )
        return numeric * (0.5 + 0.5 * fractional)

    if field == "size":
        numeric = _fraction(values, _numeric)
        small = _fraction(values, lambda value: 0 < (_as_float(value) or -1) <= 10_000)
        return numeric * small

    if field == "sizeUnit":
        return _fraction(
            values,
            lambda value: squash(value)
            in {"lots", "lot", "shares", "share", "contracts", "contract", "units"},
        )

    if field in ("setup", "rationale", "notes"):
        # Free text: not numeric, not dates, long enough to be prose. Weighted
        # down hard, or it would claim every text column in the file.
        return (
            _fraction(
                values,
                lambda value: not _numeric(value) and not _datey(value) and len(value) > 2,
            )
            * 0.35
        )

    return 0.0


def header_score(field: str, header: str) -> float:
    """How much a column's *name* looks like a given field."""
    name = squash(header)
    if not name:
        return 0.0

    best = 0.0

    for synonym in SYNONYMS[field]:
        if name == synonym:
            # Exact, and a longer synonym is more specific evidence.
            best = max(best, 1.0 + len(synonym) / 100)
        elif synonym in name:
            best = max(best, 0.6 + len(synonym) / 100)
        else:
            ratio = SequenceMatcher(None, name, synonym).ratio()
            if ratio >= FUZZY_FLOOR:
                # A typo or a truncation: "instrmnt", "symbal", "clos price".
                best = max(best, ratio * 0.75)

    return best


def analyse(headers: list[str], rows: list[list[str]]) -> dict[str, Any]:
    """Which column is which, and how sure.

    Returns a mapping of field to column index plus a per-field confidence, so
    the client can mark what was inferred rather than known — and so a person
    can see which guesses to check before anything is imported.
    """
    if not headers:
        raise ValueError("That file has no columns.")

    sample = rows[:MAX_ROWS]

    # Columns as lists of their non-empty values. Ragged rows are normal in
    # broker exports; a short row simply contributes nothing to the columns it
    # does not reach, rather than failing the file.
    samples: dict[int, list[str]] = {}
    for index in range(len(headers)):
        values = []
        for row in sample:
            if index < len(row):
                value = row[index].strip()
                if value:
                    values.append(value)
        samples[index] = values

    scored: list[tuple[float, str, int]] = []

    for field in FIELDS:
        for index, header in enumerate(headers):
            by_name = header_score(field, header)
            by_content = content_score(field, samples[index])

            # The stronger signal wins rather than the average: a column whose
            # header is unreadable but whose contents are unmistakable must not
            # be dragged under the threshold by its name.
            score = max(by_name, by_content)

            # Agreement is worth something on its own — both signals pointing
            # the same way is the case to be most confident about.
            if by_name > 0.3 and by_content > 0.3:
                score = min(score + 0.15, 1.3)

            if score >= ACCEPT:
                scored.append((score, field, index))

    scored.sort(key=lambda entry: entry[0], reverse=True)

    # The runner-up for each field, so a near-tie can be reported as one.
    best: dict[str, float] = {}
    runner_up: dict[str, float] = {}

    for score, field, _ in scored:
        if field not in best:
            best[field] = score
        elif field not in runner_up:
            runner_up[field] = score

    mapping: dict[str, int] = {}
    confidence: dict[str, float] = {}
    claimed: set[int] = set()

    for score, field, index in scored:
        if field in mapping or index in claimed:
            continue

        mapping[field] = index
        certainty = min(score, 1.0)

        # A field two columns fit almost equally well is not a confident match
        # however high the winner scored. Both may be plausible and only one
        # can be right, so the certainty is cut and the client asks.
        second = runner_up.get(field)
        if second is not None and score - second < 0.1:
            certainty = min(certainty, 0.55)

        confidence[field] = round(certainty, 2)
        claimed.add(index)

    _pair_by_time(mapping, confidence, samples)

    return {
        "mapping": mapping,
        "confidence": confidence,
        "headers": headers,
        "rows_sampled": len(sample),
    }


def _pair_by_time(
    mapping: dict[str, int],
    confidence: dict[str, float],
    samples: dict[int, list[str]],
) -> None:
    """Sort out entry and exit when the headers did not.

    A file with two date columns both called "Time" — MetaTrader writes exactly
    that — gives no way to tell open from close by name. By content it is
    obvious: the earlier one is the entry. This is the rule a person applies
    without thinking, and it is why contents beat headers more often than a
    synonym list suggests.
    """
    if "entryAt" in mapping and "exitAt" in mapping:
        # Both found, but possibly the wrong way round.
        first, second = mapping["entryAt"], mapping["exitAt"]
        here, there = _median_time(samples[first]), _median_time(samples[second])

        if here and there and here > there:
            mapping["entryAt"], mapping["exitAt"] = second, first
        return

    present = "entryAt" if "entryAt" in mapping else "exitAt" if "exitAt" in mapping else None
    if present is None:
        return

    known = mapping[present]
    missing = "exitAt" if present == "entryAt" else "entryAt"

    # One found: look for another unclaimed column that is also dates.
    claimed = set(mapping.values())
    candidates = [
        index
        for index in sorted(samples)
        if index not in claimed and content_score("entryAt", samples[index]) > 0.7
    ]

    if not candidates:
        return

    other = candidates[0]
    here, there = _median_time(samples[known]), _median_time(samples[other])
    if here is None or there is None:
        return

    # Whichever holds the earlier times is the entry, whatever either was
    # called. If the one already mapped turns out to be the later of the two,
    # the pair is assigned the other way round rather than trusting the name.
    if there > here:
        mapping["entryAt"], mapping["exitAt"] = known, other
    else:
        mapping["entryAt"], mapping["exitAt"] = other, known

    confidence[missing] = 0.6


def _median_time(values: list[str]) -> datetime | None:
    """The middle timestamp of a column, or None if it is not dates.

    The median rather than the first, because one malformed row at the top of
    an export is common and should not decide which column is the entry.

    `median_low` rather than `median`: the plain one averages the middle two on
    an even-length list, and two datetimes cannot be added.
    """
    parsed = [moment for moment in (_as_date(value) for value in values) if moment]
    if not parsed:
        return None

    # Mixed awareness would make the comparison raise. Exports are consistent
    # within a column, but a single odd value should not take the file down.
    naive = [moment.replace(tzinfo=None) for moment in parsed]
    return median_low(naive)
