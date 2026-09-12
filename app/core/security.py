"""Authentication and the request-level protections around it.

The only identity this API trusts is the session cookie it minted itself and
has just verified. A uid is never read from a path, a query string or a body —
if a client could name the account it is acting on, every ownership check in
the repositories would be decorative.

The browser holds nothing else. No ID token, no refresh token, no Firebase
config: see ``app/core/session.py`` for why the credential is a cookie script
cannot read rather than a token script can.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from fastapi import Depends, Request, params, status

from app.core import session as session_store
from app.core.config import Settings, get_settings
from app.core.errors import AppError

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CurrentUser:
    """The verified caller. Constructed only from a checked session."""

    uid: str
    email: str | None
    email_verified: bool
    name: str | None
    picture: str | None = None


class AuthError(AppError):
    def __init__(self, message: str) -> None:
        super().__init__(
            message, status_code=status.HTTP_401_UNAUTHORIZED, code="unauthorized"
        )


async def get_current_user(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> CurrentUser:
    """Verify the session cookie and return who sent it."""
    cookie = request.cookies.get(settings.session_cookie_name, "")
    if not cookie:
        raise AuthError("Sign in to continue.")

    claims = session_store.verify(cookie)

    return CurrentUser(
        uid=str(claims["uid"]),
        email=claims.get("email"),  # type: ignore[arg-type]
        email_verified=bool(claims.get("email_verified", False)),
        name=claims.get("name"),  # type: ignore[arg-type]
        picture=claims.get("picture"),  # type: ignore[arg-type]
    )


async def get_optional_user(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> CurrentUser | None:
    """The caller, if there is one.

    For the session endpoint alone: "who am I" must be able to answer "nobody"
    with a 200, or every first page load reports an error it does not have.
    """
    if not request.cookies.get(settings.session_cookie_name):
        return None
    try:
        return await get_current_user(request, settings)
    except AppError:
        return None


async def get_verified_user(
    user: CurrentUser = Depends(get_current_user),
) -> CurrentUser:
    """A caller who has confirmed their email address with us.

    Anything that writes depends on this rather than on ``get_current_user``.

    Checks the account record, not the token's ``email_verified`` claim. Google
    sets that claim itself for accounts that signed in through it, so it is true
    before anybody has clicked anything — gating on it would let a Google
    sign-up write while the app's front door still held them back. One document
    read, on writes only, is worth having the two gates agree.

    Imported here rather than at module scope: repositories import this module
    for ``CurrentUser``, and at the top that circle does not resolve.
    """
    from app.repositories import profiles

    if not profiles.is_confirmed(user.uid):
        raise AppError(
            "Confirm your email address first.",
            status_code=status.HTTP_403_FORBIDDEN,
            code="email_unverified",
        )
    return user


class RateLimiter:
    """A fixed-window limiter, keyed per caller.

    In-process on purpose: it is a guard rail for a single instance, not a
    distributed quota. Running more than one replica means moving this to
    Redis — the interface is the same, so only the storage changes.
    """

    def __init__(self, per_minute: int, *, name: str) -> None:
        self.per_minute = per_minute
        self.name = name
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str) -> None:
        now = time.monotonic()
        window = self._hits[key]

        while window and now - window[0] > 60.0:
            window.popleft()

        if len(window) >= self.per_minute:
            raise AppError(
                "Too many requests. Wait a moment and try again.",
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                code="rate_limited",
            )

        window.append(now)


_limiters: dict[str, RateLimiter] = {}


def anonymous_rate_limit(name: str, per_minute: int) -> params.Depends:
    """Limit a route that has no session to key on.

    The sign-in routes are the only ones reachable without a cookie, and also
    the ones most worth limiting — a password endpoint with no ceiling is a
    guessing machine. Keyed by client address, which is coarse: a shared IP is
    punished together, and one behind a proxy is only as trustworthy as the
    proxy. Both are acceptable for a guard rail on unauthenticated routes;
    neither would be for anything keyed to a person.
    """

    async def _dependency(request: Request) -> None:
        limiter = _limiters.get(name)
        if limiter is None:
            limiter = RateLimiter(per_minute, name=name)
            _limiters[name] = limiter

        client = request.client.host if request.client else "unknown"
        limiter.check(f"{name}:{client}")

    dependency: params.Depends = Depends(_dependency)
    return dependency


def rate_limit(
    name: str, per_minute_attr: str = "rate_limit_per_minute"
) -> Callable[..., Awaitable[None]]:
    """Build a dependency that limits one route family per user."""

    async def _dependency(
        request: Request,
        user: CurrentUser = Depends(get_current_user),
        settings: Settings = Depends(get_settings),
    ) -> None:
        limiter = _limiters.get(name)
        if limiter is None:
            limiter = RateLimiter(getattr(settings, per_minute_attr), name=name)
            _limiters[name] = limiter

        # Keyed by uid, so one noisy client cannot spend another's budget, and
        # a shared IP (an office, a mobile carrier) is not punished.
        limiter.check(f"{name}:{user.uid}")
        request.state.rate_limited_as = user.uid

    return _dependency
