"""What the CSV analyser is given, and what it answers."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: Bounds on one sample.
#:
#: A hundred rows settle what a column means as surely as ten thousand, and the
#: request cap is 256KB — so the client sends a slice, never the file.
MAX_ROWS = 200
MAX_COLUMNS = 80
MAX_CELL = 500


class CsvSample(BaseModel):
    """A sample already split into cells.

    Parsed by the client rather than here, deliberately. Its delimiter
    detection is tested and handles a case ``csv.Sniffer`` gets wrong — a
    semicolon file full of prose commas. Re-parsing here would be a second
    answer to a settled question, and the two would eventually disagree about
    the same file.
    """

    model_config = ConfigDict(extra="forbid")

    headers: list[str] = Field(min_length=1, max_length=MAX_COLUMNS)
    #: Rows may be ragged — broker exports often are — and a short row simply
    #: contributes nothing to the columns it does not reach.
    rows: list[list[str]] = Field(default_factory=list, max_length=MAX_ROWS)

    @field_validator("headers", "rows")
    @classmethod
    def _bounded(cls, value: Any) -> Any:
        """Cap the cells themselves.

        Without this the row and column limits bound the shape but not the
        size, and one row of five hundred-kilobyte cells would pass both.
        """
        rows = value if value and isinstance(value[0], list) else [value]

        for row in rows:
            if len(row) > MAX_COLUMNS:
                raise ValueError(f"No more than {MAX_COLUMNS} columns.")
            for cell in row:
                if len(cell) > MAX_CELL:
                    raise ValueError("One of those cells is too long to be a value.")

        return value


class CsvAnalysis(BaseModel):
    """Which column is which, and how sure.

    ``confidence`` is returned alongside ``mapping`` rather than folded into
    it, because the two are read differently: the mapping is what to do, the
    confidence is which parts to look at before doing it. A field guessed from
    contents alone is worth a person's glance in a way an exact header match is
    not, and the client marks those.
    """

    #: Field name to column index. Fields nothing matched are simply absent.
    mapping: dict[str, int]
    #: Field name to 0–1. Same keys as the mapping.
    confidence: dict[str, float]
    #: The header row as read, so the client can offer them for correction.
    headers: list[str]
    #: How many rows the decision was made from.
    rows_sampled: int
