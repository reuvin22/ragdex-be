"""Reading a CSV well enough to say what its columns mean.

One route, and it deliberately does not import anything. It is handed a sample
of a file and answers with a mapping — which column is the symbol, which is the
direction, and how sure it is about each.

The split is the point. Deciding what a column *means* needs to look at names
and contents together, which is work; applying that decision to ten thousand
rows is arithmetic the browser already does, tested, in
``trades/src/lib/csvFormat.ts``. Sending the whole file here to be told the
same answer would cost memory this service does not have, and would leave two
implementations of the same conversion to disagree with each other.

Nothing is stored. The sample is read, scored and forgotten inside the request.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.controllers.deps import WriteUser
from app.core.errors import AppError
from app.models.schemas.common import ErrorResponse
from app.models.schemas.csv_import import CsvAnalysis, CsvSample
from app.services import csv_analyse

router = APIRouter(
    prefix="/imports",
    tags=["imports"],
    responses={
        401: {"model": ErrorResponse},
        422: {"model": ErrorResponse, "description": "Not readable as a CSV"},
    },
)


@router.post(
    "/analyse",
    response_model=CsvAnalysis,
    summary="Work out which CSV column is which",
)
def analyse_csv(user: WriteUser, payload: CsvSample) -> CsvAnalysis:
    """Score every column against every field and return the best assignment.

    ``WriteUser`` rather than ``ReadUser``, despite writing nothing: this is
    the front half of an import, it costs real CPU per call, and the write
    limiter is the one that should govern how often a client may ask.
    """
    try:
        result = csv_analyse.analyse(payload.headers, payload.rows)
    except ValueError as error:
        # The message is the trader's to act on — an empty file, or one that is
        # not a CSV at all.
        raise AppError(str(error), status_code=422, code="unreadable_csv") from error

    return CsvAnalysis(**result)
