"""Shared route dependencies.

Re-exported from one place so a route never reaches into ``app.core`` for
auth directly — changing how a caller is identified should mean editing one
module, not every router.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends

from app.core.config import Settings, get_settings
from app.core.security import (
    CurrentUser,
    get_current_user,
    get_optional_user,
    get_verified_user,
    rate_limit,
)

# A signed-in caller. Enough to read.
ReadUser = Annotated[CurrentUser, Depends(get_current_user)]

# The caller if there is one, for the session endpoint only: "who am I" has to
# be able to answer "nobody" without that being an error.
MaybeUser = Annotated[CurrentUser | None, Depends(get_optional_user)]

# A caller who has confirmed their email. Required to write, mirroring the
# Firestore rules.
WriteUser = Annotated[CurrentUser, Depends(get_verified_user)]

AppSettings = Annotated[Settings, Depends(get_settings)]

# Route families get their own budgets: a model call costs orders of
# magnitude more than a journal read, so they cannot share one.
StandardRateLimit = Depends(rate_limit("standard"))
CoachRateLimit = Depends(rate_limit("coach", "coach_rate_limit_per_minute"))
# Candles are cheap to serve from the cache and dear to fetch, and every miss
# spends provider quota — so this sits below the standard budget rather than
# sharing it.
MarketRateLimit = Depends(rate_limit("market", "market_rate_limit_per_minute"))
